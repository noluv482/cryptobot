#!/usr/bin/env python3
"""Overnight strategy-research lab — paper/backtest ONLY, mechanical, no opinions.

WHAT THIS IS (and is NOT)
-------------------------
An automated version of the manual research loop that produced find_signal.py:
keep the multi-year history corpus fresh, sweep parameterized versions of the
same 15 candidate signal families over the FULL universe with the exact same
statistical discipline, and mechanically nominate lever-configs for autopilot's
paper challenger array. It holds NO market opinions, invents NO indicators, and
makes NO profit claims. The expected — and honestly reported — nightly result
is "0 cleared the bar": hourly crypto is close to efficient and these are the
most-mined ideas in existence. The lab exists so that IF a regime ever makes
one of them work, the bot notices without a human re-running scripts.

DISCIPLINE (inherited verbatim from find_signal.py — see its docstring)
-----------------------------------------------------------------------
  1. FULL UNIVERSE — every pair with history, never a hand-picked subset.
  2. SPLIT BY TIME — first half of EACH pair ranks (in-sample), second half
     judges (out-of-sample), fractions per pair so a 9-year BTC file is not
     truncated to match a 1-year PUMP file.
  3. DIRECTION-MATCHED BASELINE — each side scored against its own baseline,
     not buy-and-hold.
  4. MULTIPLE COMPARISONS — |t_is| > 2 AND |t_oos| > 2.5, same sign.
  5. NO LOOKAHEAD — every signal sees data up to and including bar i only.
PLUS the fee honesty find_signal only printed as a warning: a survivor must
also have edge_oos > ROUND_TRIP_COST_PCT (net_oos_edge_after_cost > 0), which
also means an "inverted" (negative-edge) survivor never clears — a rule that
only works backwards is reported, never nominated.
PLUS a purged split find_signal lacked: the in-sample window ends `horizon`
bars BEFORE the midpoint, so no IS forward-return label ever reads a price
from the half that later judges the candidate (see _sweep_pair).

WHY A RE-IMPLEMENTATION AND NOT `import find_signal`
----------------------------------------------------
find_signal's candidates are deliberately simple closures that slice
`c[:i+1]` per bar — O(n) work per bar, O(n^2) per pair, hours for a full-grid
run. This module computes the same signals from incrementally-precomputed
arrays (rolling sums, monotonic-deque extremes, Wilder RSI carried forward)
so the whole ~1.4M-row corpus sweeps in minutes. Every original candidate's
default parameters are a point inside its grid here, so this is a superset,
not a drift.

HARD SANDBOX INVARIANTS (this module can NEVER touch money)
-----------------------------------------------------------
  * Never constructs a PaperTrader, never references any order/private
    endpoint, never reads/writes/clears PAPER_LOCK. It only uses bot_server
    CONSTANTS (SCAN_UNIVERSE, ROUND_TRIP_COST_PCT, RSI_PERIOD), log() and tg().
  * All writes are confined to DATA_DIR: research_state.json (ledger),
    lab_challengers.json (nominations), research.lock, and history/ CSVs.
  * Nominations are DATA ONLY — autopilot.py sanitizes and instantiates them
    itself as PaperTrader(force_paper=True, no_persist=True) challengers; a
    corrupted nominations file can therefore never escalate past paper.
  * Every phase and every pair is exception-wrapped: one bad CSV or one dead
    API loses that pair's night, never the cycle.

FILES (shapes are a cross-module contract — autopilot.py and bot_server's
/research endpoint read them; change nothing casually)
  {DATA_DIR}/research_state.json   ledger, written ONLY here
  {DATA_DIR}/lab_challengers.json  nominations, written ONLY here, read by autopilot
  {DATA_DIR}/research.lock         {"pid":int,"ts":float}; stale after 3h
  {DATA_DIR}/history/{PAIR}_60.csv fetch_history.py format (ts,open,high,low,close,volume)

Usage:
    python research_lab.py --cycle          # nightly: backfill -> sweep -> nominate -> digest
    python research_lab.py --status         # print ledger summary
    python research_lab.py --backfill-only  # just top up history CSVs
"""
import argparse
import bisect
import csv
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

import bot_server as bs

# fetch_history.py is the single source of truth for HOW candles are fetched
# (Coinbase 300-candle windows, Binance.US fallback, polite sleeps). Import it
# when possible; a minimal Coinbase-only replica below covers the import-failed
# case so the lab still converges on a box where that file is missing.
try:
    import fetch_history as _fh
except Exception:
    _fh = None


def _truthy(name, default=""):
    """Env flag parser — the same truthy set the rest of the deploy uses."""
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return int(default)


# ── Config (env-tunable; defaults chosen for a nightly 45-min window) ─────────
RESEARCH_LAB_ENABLED = _truthy("RESEARCH_LAB")        # scheduler gate (bot_server checks it)
RESEARCH_HOUR_UTC    = _env_int("RESEARCH_HOUR_UTC", 4)
RESEARCH_BUDGET_MINS = float(_env_int("RESEARCH_BUDGET_MINS", 45))
# How many pairs to (re)fetch per cycle. 4/night walks the whole universe in
# ~9 nights without hammering public APIs; a fresh server converges gradually.
BACKFILL_PAIRS_PER_CYCLE = _env_int("RESEARCH_BACKFILL_PAIRS", 4)

DATA_DIR         = bs._DATA_DIR
HIST_DIR         = os.path.join(DATA_DIR, "history")
STATE_PATH       = os.path.join(DATA_DIR, "research_state.json")
CHALLENGERS_PATH = os.path.join(DATA_DIR, "lab_challengers.json")
LOCK_PATH        = os.path.join(DATA_DIR, "research.lock")

LOCK_STALE_SECS  = 3 * 3600     # a cycle is budgeted in minutes; 3h-old lock = crashed run
HIST_STALE_SECS  = 24 * 3600    # nightly cadence: refreshed less than a day ago = fresh
WARMUP           = 80           # bars skipped at each pair's start (find_signal's warmup)
HORIZONS         = (6, 24)      # forward-return horizons in bars (1h candles)
MIN_TRADES       = 30           # find_signal's floor: below this a t-stat is a coin flip
T_IS_BAR         = 2.0          # in-sample significance bar
T_OOS_BAR        = 2.5          # out-of-sample bar (Bonferroni-ish, ~130 tests)
RUNS_KEPT        = 30           # ledger keeps the last N run summaries

# ── Lever-nomination space (mirrors autopilot's intake sanitization EXACTLY;
#    anything outside these bounds would be clamped there anyway) ─────────────
MAX_SLOTS        = 3
RETIRE_MIN_N     = 40           # trades before a slot can be called a proven loser
RETIRE_DAYS      = 21           # max age without clearing autopilot's cost gate
LEVER_CONF_LO, LEVER_CONF_HI = 0.28, 0.90
LEVER_RR_LO,   LEVER_RR_HI   = 0.5, 5.0
LEVER_RISK_LO, LEVER_RISK_HI = 0.01, 0.50
STRATEGY_KEYS = frozenset({
    "MULTI_SIGNAL", "MOMENTUM_BREAKOUT", "TREND_CONTINUATION", "RSI_REVERSAL",
    "NEWS_CATALYST", "PATTERN_BREAKOUT", "CONFLUENCE", "LEARNING_SIGNAL",
})


def _log(msg, level="INFO"):
    bs.log("LAB", msg, level)


# ── Ledger + lock + atomic JSON ──────────────────────────────────────────────
def _default_state():
    return {"version": 1, "last_run_ts": 0.0, "running": False,
            "cursor": {"phase": "idle", "pair_idx": 0, "family_idx": 0},
            "backfill": {}, "runs": [], "family_results": {}, "last_digest": ""}


def _atomic_json(path, obj):
    """tmp + os.replace so a reader (bot_server's /research, autopilot's intake)
    never sees a half-written file, even on Windows."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _load_state():
    """Ledger or a fresh default — a corrupt ledger costs history, never a crash."""
    st = _default_state()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            st.update(got)
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"ledger unreadable ({e}) — starting fresh", "WARN")
    return st


def _save_state(st):
    try:
        _atomic_json(STATE_PATH, st)
    except Exception as e:
        _log(f"ledger save failed: {e}", "ERR")


def _take_lock():
    """True if we own the lock. A lock younger than 3h means another cycle is
    genuinely running (the scheduler could double-fire across a redeploy);
    older is a crashed run and is taken over."""
    try:
        with open(LOCK_PATH, encoding="utf-8") as f:
            held = json.load(f)
        if time.time() - float(held.get("ts", 0)) < LOCK_STALE_SECS:
            _log(f"lock held by pid {held.get('pid')} — skipping this cycle")
            return False
        _log("stale lock (>3h) — taking over", "WARN")
    except FileNotFoundError:
        pass
    except Exception:
        pass                     # unreadable lock == stale lock
    try:
        _atomic_json(LOCK_PATH, {"pid": os.getpid(), "ts": time.time()})
        return True
    except Exception as e:
        _log(f"cannot write lock: {e}", "ERR")
        return False


def _release_lock():
    try:
        with open(LOCK_PATH, encoding="utf-8") as f:
            held = json.load(f)
        if int(held.get("pid", -1)) == os.getpid():
            os.remove(LOCK_PATH)
    except Exception:
        pass                     # never let cleanup kill the exit path


# ── History corpus ───────────────────────────────────────────────────────────
def _hist_path(pair):
    return os.path.join(HIST_DIR, f"{pair}_60.csv")


def _load_pair(pair):
    """(closes, highs, lows, vols) from the corpus CSV, or None.

    Mirrors find_signal.load_history: same >500-row floor, same tolerance of
    junk lines. Adds a close>0 guard because every downstream ratio divides by
    a close and one zero row must cost a row, not the pair."""
    path = _hist_path(pair)
    if not os.path.exists(path):
        return None
    c, h, l, v = [], [], [], []
    with open(path, encoding="utf-8") as f:
        next(f, None)                              # header
        for line in f:
            bits = line.rstrip("\n").split(",")
            if len(bits) < 6 or not bits[0].isdigit():
                continue
            try:
                cl = float(bits[4])
                if cl <= 0:
                    continue
                c.append(cl); h.append(float(bits[2]))
                l.append(float(bits[3])); v.append(float(bits[5]))
            except ValueError:
                continue
    return (c, h, l, v) if len(c) > 500 else None


def _last_ts(path):
    """Last candle timestamp in a corpus CSV (tail read, same as fetch_history)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-200, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode(errors="replace").strip().splitlines()
        for line in reversed(tail):
            bits = line.split(",")
            if bits and bits[0].isdigit():
                return int(bits[0])
    except OSError:
        pass
    return None


# ── Backfill (fetch_history's approach, budget-aware) ────────────────────────
_CB_API   = "https://api.exchange.coinbase.com"
_CB_SPEC  = {"XBT": "BTC", "XDG": "DOGE"}
_GENESIS  = 1483228800          # 2017-01-01, matches fetch_history
_CB_WIN   = 300 * 3600          # Coinbase max 300 candles/request


def _cb_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-research"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _pull_coinbase_fallback(pair, writer, resume):
    """Compact replica of fetch_history.pull_coinbase, used ONLY when that
    module cannot be imported. Same windowed walk, same CSV row shape, same
    None-means-no-product convention."""
    product = f"{_CB_SPEC.get(pair[:-3], pair[:-3])}-USD"
    try:
        _cb_get(f"{_CB_API}/products/{product}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    start = (resume + 3600) if resume else _GENESIS
    now = int(time.time())
    rows = 0
    while start < now:
        end = min(start + _CB_WIN, now)
        q = urllib.parse.urlencode({
            "granularity": 3600,
            "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            "end":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))})
        for attempt in range(5):
            try:
                batch = _cb_get(f"{_CB_API}/products/{product}/candles?{q}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + attempt * 2)
                    continue
                raise
        else:
            batch = []
        # newest-first [time, low, high, open, close, volume] -> ascending OHLCV
        for cndl in sorted(batch, key=lambda x: x[0]):
            t = int(cndl[0])
            if t <= (resume or 0) or t + 3600 > now:   # dupes + still-forming bar
                continue
            writer.writerow([t, cndl[3], cndl[2], cndl[1], cndl[4], cndl[5]])
            resume = t
            rows += 1
        start = end
        time.sleep(0.25)
    return rows


def _backfill_candidates(state):
    """Pairs worth fetching, most-valuable first: never-tried missing pairs,
    then existing files oldest-first, then known-no-source pairs (backfill
    ledger value 0) dead last so XMR/HYPE/TRX-style gaps cannot eat the nightly
    slots forever. Stateless priority — no cursor needed to converge."""
    now = time.time()
    missing, stale, dead = [], [], []
    for cfg in bs.SCAN_UNIVERSE:
        pair = cfg["pair"]
        lt = _last_ts(_hist_path(pair))
        if lt is None:
            (dead if state["backfill"].get(pair) == 0 else missing).append(pair)
        elif lt < now - HIST_STALE_SECS:
            stale.append((lt, pair))
    stale.sort()
    return missing + [p for _, p in stale] + dead


def _backfill_pair(pair):
    """Fetch/extend one corpus CSV. Returns (new_rows, had_source)."""
    path = _hist_path(pair)
    resume = _last_ts(path)
    mode = "a" if resume else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
        if _fh is not None:
            n = _fh.pull_coinbase(pair, w, resume)
            if n is None:
                n = _fh.pull_binanceus(pair, w, resume)
        else:
            n = _pull_coinbase_fallback(pair, w, resume)
    if n is None:
        # No source anywhere. Remove a header-only stub so "missing" detection
        # stays truthful (mirrors fetch_history.main's cleanup).
        if mode == "w":
            try:
                os.remove(path)
            except OSError:
                pass
        return 0, False
    return n, True


def _phase_backfill(state, deadline):
    """Top up at most BACKFILL_PAIRS_PER_CYCLE corpus files. Budget is checked
    BETWEEN pairs (a single fresh 9-year fetch may legitimately run minutes);
    every pair is exception-wrapped so one dead API never kills the cycle."""
    info = {"tried": 0, "new_rows": 0, "failed": 0}
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        todo = _backfill_candidates(state)[:max(0, BACKFILL_PAIRS_PER_CYCLE)]
        for i, pair in enumerate(todo):
            if time.monotonic() > deadline:
                _log(f"backfill stopped by budget after {i} pairs")
                break
            state["cursor"] = {"phase": "backfill", "pair_idx": i, "family_idx": 0}
            try:
                n, ok = _backfill_pair(pair)
                info["tried"] += 1
                info["new_rows"] += n
                state["backfill"][pair] = int(_last_ts(_hist_path(pair)) or 0) if ok else 0
                _log(f"backfill {pair}: +{n:,} candles" + ("" if ok else " (no source)"))
            except Exception as e:
                info["failed"] += 1
                _log(f"backfill {pair} failed: {e}", "WARN")
            _save_state(state)          # resumable pair-by-pair
    except Exception as e:
        _log(f"backfill phase died: {e}", "ERR")
    return info


# ── Family grid ──────────────────────────────────────────────────────────────
# The 15 find_signal CANDIDATES collapse into 11 parameterized families (its
# reversal_1/6/24, zscore_24/48 and momentum_24/72 entries were already just
# parameter variants). Every original default is a grid point. Grids are
# MECHANICAL neighborhoods around those defaults — no curated "good" values,
# because a value chosen for looking good is the first step of overfitting.
def _build_grid():
    g = []
    for n in (1, 6, 24):                                  # reversal: fade the n-bar move
        for thr in (0.002, 0.004, 0.008):
            g.append((f"reversal_n{n}_t{int(thr*10000)}bp", "reversal",
                      {"n": n, "thr": thr}))
    for w in (24, 48, 96):                                # zscore fade vs own mean
        for z in (1.0, 1.5, 2.0):
            g.append((f"zscore_w{w}_z{z:g}", "zscore_fade", {"window": w, "z": z}))
    for w in (12, 24, 72):                                # momentum continuation
        for thr in (0.005, 0.01, 0.02):
            g.append((f"momentum_w{w}_t{int(thr*10000)}bp", "momentum",
                      {"window": w, "thr": thr}))
    for fast in (8, 12):                                  # MA crossover event
        for slow in (24, 48, 96):
            g.append((f"macross_f{fast}_s{slow}", "ma_cross",
                      {"fast": fast, "slow": slow}))
    for lb in (12, 24, 48):                               # Donchian breakout
        g.append((f"breakout_lb{lb}", "breakout", {"lookback": lb}))
    for w in (12, 24, 48):                                # range-expansion direction
        for m in (1.5, 2.0, 3.0):
            g.append((f"volexp_w{w}_m{m:g}", "vol_expansion", {"window": w, "mult": m}))
    for w in (24, 48):                                    # abnormal-volume fade
        for m in (3.0, 5.0):
            for thr in (0.003, 0.006):
                g.append((f"volfade_w{w}_m{m:g}_t{int(thr*10000)}bp",
                          "volume_spike_fade", {"window": w, "mult": m, "thr": thr}))
    for hi, lo in ((70, 30), (75, 25), (80, 20)):         # RSI extremes
        g.append((f"rsi_{hi}_{lo}", "rsi_extreme", {"hi": hi, "lo": lo}))
    for k in (1, 2):                                      # inside-bar resolution
        g.append((f"inside_k{k}", "inside_bar", {"k": k}))
    for thr in (0.010, 0.015, 0.025):                     # large-bar fade
        g.append((f"gapfade_t{int(thr*10000)}bp", "gap_fade", {"thr": thr}))
    for k in (3, 4, 5):                                   # k-in-a-row fade
        g.append((f"runfade_k{k}", "three_in_a_row", {"k": k}))
    return g


_GRID     = _build_grid()
_FAMILIES = sorted({fam for _, fam, _ in _GRID})
_GRID_SIG = f"{len(_GRID)}x{len(HORIZONS)}-purged"   # invalidates a resumed sweep if the grid OR window semantics changed
_RET_NS   = (1, 6, 12, 24, 72)
_SMA_WS   = (8, 12, 24, 48, 96)
_Z_WS     = (24, 48, 96)
_DON_WS   = (12, 24, 48)
_RNG_WS   = (12, 24, 48)
_VMED_WS  = (24, 48)


# ── Per-pair precomputation (this is the O(n) speed trick) ───────────────────
def _prefix(xs):
    out = [0.0] * (len(xs) + 1)
    acc = 0.0
    for i, x in enumerate(xs):
        acc += x
        out[i + 1] = acc
    return out


def _roll_max_prev(xs, n):
    """out[i] = max(xs[i-n:i]) — the PREVIOUS n bars, excluding i, exactly like
    sig_breakout_24's slice. Monotonic deque: O(n) total."""
    out = [None] * len(xs)
    dq = deque()
    for i in range(len(xs)):
        while dq and dq[0] < i - n:
            dq.popleft()
        if i >= n:
            out[i] = xs[dq[0]]
        while dq and xs[dq[-1]] <= xs[i]:
            dq.pop()
        dq.append(i)
    return out


def _roll_min_prev(xs, n):
    out = [None] * len(xs)
    dq = deque()
    for i in range(len(xs)):
        while dq and dq[0] < i - n:
            dq.popleft()
        if i >= n:
            out[i] = xs[dq[0]]
        while dq and xs[dq[-1]] >= xs[i]:
            dq.pop()
        dq.append(i)
    return out


def _roll_median_prev(xs, n):
    """out[i] = median(xs[i-n:i]) via a sorted sliding window; n is 24/48 so the
    insort/pop memmove is trivial. statistics.median's even-n mean is matched."""
    out = [None] * len(xs)
    win = []
    for i in range(len(xs)):
        if i > 0:
            bisect.insort(win, xs[i - 1])
        if i - n - 1 >= 0:
            win.pop(bisect.bisect_left(win, xs[i - n - 1]))
        if i >= n:
            m = n // 2
            out[i] = win[m] if n % 2 else 0.5 * (win[m - 1] + win[m])
    return out


def _precompute(c, h, l, v):
    """Every array any family needs, each built in one pass over the pair."""
    n = len(c)
    pre = {"n": n}
    ret = {}
    for lag in _RET_NS:                               # lag-return series
        arr = [None] * n
        for i in range(lag, n):
            prev = c[i - lag]
            arr[i] = (c[i] - prev) / prev
        ret[lag] = arr
    pre["ret"] = ret
    P = _prefix(c)
    Q = _prefix([x * x for x in c])
    sma = {}
    for w in _SMA_WS:                                 # rolling means incl. bar i
        arr = [None] * n
        for i in range(w - 1, n):
            arr[i] = (P[i + 1] - P[i + 1 - w]) / w
        sma[w] = arr
    pre["sma"] = sma
    zsc = {}
    for w in _Z_WS:                                   # z of close vs own window
        arr = [None] * n
        for i in range(w - 1, n):
            m = (P[i + 1] - P[i + 1 - w]) / w
            var = (Q[i + 1] - Q[i + 1 - w]) / w - m * m
            sd = math.sqrt(var) if var > 0 else 0.0
            arr[i] = (c[i] - m) / sd if sd > 1e-12 else None
        zsc[w] = arr
    pre["z"] = zsc
    pre["dmax"] = {w: _roll_max_prev(h, w) for w in _DON_WS}
    pre["dmin"] = {w: _roll_min_prev(l, w) for w in _DON_WS}
    rng = [(h[i] - l[i]) / c[i] for i in range(n)]    # bar range as fraction of close
    pre["rng"] = rng
    R = _prefix(rng)
    rngm = {}
    for w in _RNG_WS:                                 # mean range of previous w bars
        arr = [None] * n
        for i in range(w, n):
            arr[i] = (R[i] - R[i - w]) / w
        rngm[w] = arr
    pre["rngm"] = rngm
    pre["vmed"] = {w: _roll_median_prev(v, w) for w in _VMED_WS}
    # Wilder RSI carried forward — replicates bs.calc_rsi(c[:i+1]) EXACTLY,
    # including its quirk of seeding with diffs 1..14 and then re-applying diff
    # 14 in the smoothing loop. Fidelity to the original beats textbook Wilder:
    # the point is to test the SAME rule the bot uses.
    period = bs.RSI_PERIOD
    rsi = [None] * n
    if n > period:
        gains = losses = 0.0
        for i in range(1, period + 1):
            d = c[i] - c[i - 1]
            gains += max(d, 0); losses += max(-d, 0)
        ag = gains / period
        al = losses / period
        for i in range(period, n):
            d = c[i] - c[i - 1]
            ag = (ag * (period - 1) + max(d, 0)) / period
            al = (al * (period - 1) + max(-d, 0)) / period
            if al == 0 and ag == 0:
                rsi[i] = 50.0
            elif al > 0:
                rsi[i] = round(100 - 100 / (1 + ag / al), 1)
            else:
                rsi[i] = 100.0
    pre["rsi"] = rsi
    dn = [0] * n                                       # consecutive down/up close runs
    up = [0] * n
    for i in range(1, n):
        if c[i] < c[i - 1]:
            dn[i] = dn[i - 1] + 1
        elif c[i] > c[i - 1]:
            up[i] = up[i - 1] + 1
    pre["dn"] = dn
    pre["up"] = up
    fwd = {}
    for hz in HORIZONS:                                # forward long return per bar
        arr = [None] * n
        for i in range(n - hz):
            arr[i] = (c[i + hz] - c[i]) / c[i]
        fwd[hz] = arr
    pre["fwd"] = fwd
    return pre


# ── Family evaluators ────────────────────────────────────────────────────────
# Each returns (n_buy, sum_buy, sumsq_buy, n_sell, sum_sell, sumsq_sell) over
# [a2, b), where sell returns are already direction-flipped (fwd of a SELL is
# -fwd of a BUY, exactly find_signal.fwd). Guards (a2) reproduce each original
# candidate's "if i < ...: return None" so warmup semantics match at every
# parameterization, not just the defaults.
def _move_rule(rn, thr, fade, a2, b, fwd):
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(a2, b):
        r = rn[i]
        if r > thr:
            f = fwd[i]
            if fade:
                g = -f; ns_ += 1; ss_ += g; qs_ += g * g
            else:
                nb += 1; sb += f; qb += f * f
        elif r < -thr:
            f = fwd[i]
            if fade:
                nb += 1; sb += f; qb += f * f
            else:
                g = -f; ns_ += 1; ss_ += g; qs_ += g * g
    return nb, sb, qb, ns_, ss_, qs_


def _ev_reversal(pre, c, h, l, v, p, a, b, fwd):
    lag = p["n"]
    return _move_rule(pre["ret"][lag], p["thr"], True, max(a, lag + 1), b, fwd)


def _ev_momentum(pre, c, h, l, v, p, a, b, fwd):
    w = p["window"]
    return _move_rule(pre["ret"][w], p["thr"], False, max(a, w + 1), b, fwd)


def _ev_zscore(pre, c, h, l, v, p, a, b, fwd):
    zarr = pre["z"][p["window"]]
    k = p["z"]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, p["window"] - 1), b):
        z = zarr[i]
        if z is None:
            continue
        if z > k:                                      # stretched high -> fade short
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
        elif z < -k:
            f = fwd[i]; nb += 1; sb += f; qb += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_ma_cross(pre, c, h, l, v, p, a, b, fwd):
    # Fidelity note (measured on SOLUSD, 45k bars): prefix-sum SMAs can round
    # differently from find_signal's per-bar sum(xs[-n:]) at an EXACT fast==slow
    # tie (diff 0.0 vs -5e-12), which moves that cross detection by one bar.
    # Observed effect: 2 of 60 test windows, same event count, edge delta ~1e-5,
    # t delta ~0.01 — three orders below the |t|>2/2.5 bars, so the discipline
    # is unaffected. Bit-exactness would need the O(n^2) per-bar re-sum this
    # module exists to eliminate.
    fa = pre["sma"][p["fast"]]
    sl = pre["sma"][p["slow"]]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, p["slow"] + 1), b):
        fv, sv, pf, ps = fa[i], sl[i], fa[i - 1], sl[i - 1]
        if pf <= ps and fv > sv:                       # cross up event
            f = fwd[i]; nb += 1; sb += f; qb += f * f
        elif pf >= ps and fv < sv:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_breakout(pre, c, h, l, v, p, a, b, fwd):
    lb = p["lookback"]
    hiw = pre["dmax"][lb]
    low = pre["dmin"][lb]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, lb + 1), b):
        ci = c[i]
        if ci > hiw[i]:                                # Donchian up-break
            f = fwd[i]; nb += 1; sb += f; qb += f * f
        elif ci < low[i]:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_vol_expansion(pre, c, h, l, v, p, a, b, fwd):
    w = p["window"]
    mult = p["mult"]
    rng = pre["rng"]
    rm = pre["rngm"][w]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, w + 2), b):
        if rng[i] < mult * rm[i]:                      # not an expansion bar
            continue
        if c[i] > (h[i] + l[i]) / 2:                   # direction of the break
            f = fwd[i]; nb += 1; sb += f; qb += f * f
        else:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_volume_spike_fade(pre, c, h, l, v, p, a, b, fwd):
    w = p["window"]
    mult = p["mult"]
    thr = p["thr"]
    med = pre["vmed"][w]
    r1 = pre["ret"][1]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, w + 2), b):
        vi = v[i]
        if not vi:
            continue
        m = med[i]
        if m is None or m <= 0 or vi < mult * m:       # not an abnormal-volume bar
            continue
        r = r1[i]
        if r > thr:                                    # blowoff -> fade short
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
        elif r < -thr:                                 # capitulation -> fade long
            f = fwd[i]; nb += 1; sb += f; qb += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_rsi_extreme(pre, c, h, l, v, p, a, b, fwd):
    rsi = pre["rsi"]
    hi_t = p["hi"]
    lo_t = p["lo"]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, 30), b):                     # original's i<30 guard
        r = rsi[i]
        if r is None:
            continue
        if r > hi_t:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
        elif r < lo_t:
            f = fwd[i]; nb += 1; sb += f; qb += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_inside_bar(pre, c, h, l, v, p, a, b, fwd):
    k = p["k"]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, k + 2), b):
        ok = True
        for j in range(1, k + 1):                      # k consecutive inside bars
            if not (h[i - j] < h[i - j - 1] and l[i - j] > l[i - j - 1]):
                ok = False
                break
        if not ok:
            continue
        if c[i] > h[i - 1]:                            # resolution direction
            f = fwd[i]; nb += 1; sb += f; qb += f * f
        elif c[i] < l[i - 1]:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_gap_fade(pre, c, h, l, v, p, a, b, fwd):
    thr = p["thr"]
    r1 = pre["ret"][1]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, 2), b):
        r = r1[i]
        if r >= thr:                                   # big up bar -> fade short
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
        elif r <= -thr:
            f = fwd[i]; nb += 1; sb += f; qb += f * f
    return nb, sb, qb, ns_, ss_, qs_


def _ev_three_in_a_row(pre, c, h, l, v, p, a, b, fwd):
    k = p["k"]
    dn = pre["dn"]
    up = pre["up"]
    nb = ns_ = 0
    sb = qb = ss_ = qs_ = 0.0
    for i in range(max(a, k + 1), b):
        if dn[i] >= k:                                 # k straight down closes -> fade long
            f = fwd[i]; nb += 1; sb += f; qb += f * f
        elif up[i] >= k:
            f = -fwd[i]; ns_ += 1; ss_ += f; qs_ += f * f
    return nb, sb, qb, ns_, ss_, qs_


_EVALS = {
    "reversal":          _ev_reversal,
    "zscore_fade":       _ev_zscore,
    "momentum":          _ev_momentum,
    "ma_cross":          _ev_ma_cross,
    "breakout":          _ev_breakout,
    "vol_expansion":     _ev_vol_expansion,
    "volume_spike_fade": _ev_volume_spike_fade,
    "rsi_extreme":       _ev_rsi_extreme,
    "inside_bar":        _ev_inside_bar,
    "gap_fade":          _ev_gap_fade,
    "three_in_a_row":    _ev_three_in_a_row,
}


# ── Scoring (find_signal.score, computed from running sums) ──────────────────
def _score(accw, base_n, base_sum):
    """(n, edge, t) vs the direction-matched baseline.

    Algebraically identical to find_signal.score's list version: adjusted
    returns are buy-bl and sell+bl; mean and POPULATION sd fall straight out of
    the per-side (count, sum, sumsq) triples, so no per-trade lists are kept."""
    nb, sb, qb, ns_, ss_, qs_ = accw
    n = int(nb) + int(ns_)
    if n < MIN_TRADES or base_n <= 0:
        return n, None, None
    bl = base_sum / base_n
    s_adj = (sb - nb * bl) + (ss_ + ns_ * bl)
    q_adj = (qb - 2 * bl * sb + nb * bl * bl) + (qs_ + 2 * bl * ss_ + ns_ * bl * bl)
    m = s_adj / n
    var = q_adj / n - m * m
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd < 1e-12:
        return n, None, None
    return n, m, m / (sd / math.sqrt(n))


# ── Sweep phase ──────────────────────────────────────────────────────────────
def _fresh_partial():
    """Accumulators for a new sweep. Kept JSON-serializable so a budget-stopped
    sweep persists mid-flight and resumes the next night with nothing lost."""
    acc = {}
    for slug, _, _ in _GRID:
        for hz in HORIZONS:
            acc[f"{slug}_h{hz}"] = {"is": [0, 0.0, 0.0, 0, 0.0, 0.0],
                                    "oos": [0, 0.0, 0.0, 0, 0.0, 0.0]}
    base = {f"h{hz}_{w}": [0, 0.0] for hz in HORIZONS for w in ("is", "oos")}
    pairs = [cfg["pair"] for cfg in bs.SCAN_UNIVERSE
             if os.path.exists(_hist_path(cfg["pair"]))]
    return {"sig": _GRID_SIG, "pairs_remaining": pairs, "pairs_done": 0,
            "pairs_ok": 0, "acc": acc, "base": base}


def _sweep_pair(pair, part):
    """Evaluate the WHOLE grid on one pair and fold into the accumulators.
    Pair-major order keeps peak memory to one pair's arrays (~150k bars) rather
    than the whole 1.4M-row corpus. Returns False if the pair was unusable."""
    data = _load_pair(pair)
    if data is None:
        return False
    c, h, l, v = data
    pre = _precompute(c, h, l, v)
    n = len(c)
    acc = part["acc"]
    base = part["base"]
    for hz in HORIZONS:
        fwd = pre["fwd"][hz]
        for wname, lo, hi in (("is", 0.0, 0.5), ("oos", 0.5, 1.0)):
            # Same window arithmetic as find_signal.evaluate — fractions of THIS
            # pair's length, warmup 80, last bar leaving room for the horizon —
            # PLUS a purge find_signal lacked: its IS window ran right up to the
            # midpoint, so the last hz in-sample bars' forward returns were read
            # from OOS-half prices (verified: perturbing only the first 24 OOS
            # bars of SOLUSD moved 79/128 IS accumulators). Ending the IS window
            # hz bars early means NO in-sample label ever consults data the OOS
            # half will later judge with. Slightly fewer IS bars, strictly
            # cleaner split.
            a = max(WARMUP, int(n * lo))
            b = min(int(n * hi), n - hz - 1)
            if hi < 1.0:
                b = min(b, int(n * hi) - hz)
            if b <= a:
                continue
            bacc = base[f"h{hz}_{wname}"]
            bacc[0] += b - a
            bacc[1] += math.fsum(fwd[a:b])
            for slug, fam, params in _GRID:
                res = _EVALS[fam](pre, c, h, l, v, params, a, b, fwd)
                slot = acc[f"{slug}_h{hz}"][wname]
                for j in range(6):
                    slot[j] += res[j]
    return True


def _finalize_sweep(state, part):
    """Score every test and write family_results. Only tests where BOTH halves
    produced a defined t are recorded (find_signal drops the rest too) — a
    survivor additionally needs the fee-honesty gate find_signal only printed."""
    results = {}
    survivors = []
    now = time.time()
    for slug, fam, params in _GRID:
        for hz in HORIZONS:
            tid = f"{slug}_h{hz}"
            e = part["acc"][tid]
            n_i, e_i, t_i = _score(e["is"], *part["base"][f"h{hz}_is"])
            n_o, e_o, t_o = _score(e["oos"], *part["base"][f"h{hz}_oos"])
            if t_i is None or t_o is None:
                continue
            net = e_o - bs.ROUND_TRIP_COST_PCT
            clears = (abs(t_i) > T_IS_BAR and abs(t_o) > T_OOS_BAR
                      and (t_i > 0) == (t_o > 0) and net > 0)
            results[tid] = {"family": fam, "params": params, "horizon": hz,
                            "n_is": n_i, "n_oos": n_o,
                            "t_is": round(t_i, 3), "t_oos": round(t_o, 3),
                            "edge_is": e_i, "edge_oos": e_o,
                            "net_oos_edge_after_cost": net,
                            "clears": clears, "ts": now}
            if clears:
                survivors.append(tid)
    state["family_results"] = results
    return survivors


def _phase_sweep(state, deadline):
    """Family sweep with cursor save/resume. pair_idx in the cursor counts
    pairs COMPLETED this sweep; family_idx stays 0 because the pair-major loop
    always finishes every family for a pair before moving on."""
    out = {"complete": False, "pairs_used": 0, "survivors": [], "note": ""}
    try:
        # Resume is keyed on sweep_partial ALONE (popped on completion, sig-
        # stamped against grid edits) — NOT on cursor.phase, which the backfill
        # phase legitimately overwrites every night before this phase runs.
        part = state.get("sweep_partial")
        resume = (isinstance(part, dict) and part.get("sig") == _GRID_SIG
                  and isinstance(part.get("pairs_remaining"), list))
        if not resume:
            part = _fresh_partial()
        else:
            _log(f"resuming sweep: {len(part['pairs_remaining'])} pairs left")
        while part["pairs_remaining"]:
            if time.monotonic() > deadline:
                # Budget hit BETWEEN pairs: persist accumulators + cursor and
                # let the next night finish. Nothing computed is thrown away.
                state["sweep_partial"] = part
                state["cursor"] = {"phase": "sweep",
                                   "pair_idx": part["pairs_done"], "family_idx": 0}
                out["note"] = (f"paused by budget with "
                               f"{len(part['pairs_remaining'])} pairs left")
                _save_state(state)
                return out
            pair = part["pairs_remaining"][0]
            try:
                if _sweep_pair(pair, part):
                    part["pairs_ok"] += 1
                else:
                    _log(f"sweep skipped {pair}: no usable history", "WARN")
            except Exception as e:
                _log(f"sweep {pair} failed: {e}", "WARN")
            part["pairs_remaining"].pop(0)
            part["pairs_done"] += 1
        out["survivors"] = _finalize_sweep(state, part)
        out["complete"] = True
        out["pairs_used"] = part["pairs_ok"]
        state.pop("sweep_partial", None)
        state["cursor"] = {"phase": "idle", "pair_idx": 0, "family_idx": 0}
        _save_state(state)
    except Exception as e:
        _log(f"sweep phase died: {e}", "ERR")
        out["note"] = f"sweep error: {e}"
    return out


# ── Lever nominations ────────────────────────────────────────────────────────
def _sanitize_lever_config(cfg):
    """Clamp a nomination into the EXACT legal space autopilot's intake
    enforces. Redundant on purpose: this file is data crossing a module
    boundary and both sides validating is what keeps a bug in one of them from
    ever widening a risk band."""
    cid = str(cfg.get("id", ""))
    if not cid.startswith("lab_") or cid == "base":
        return None
    ecf = min(LEVER_CONF_HI, max(LEVER_CONF_LO, float(cfg.get("entry_conf_floor", LEVER_CONF_LO))))
    mrr = cfg.get("min_rr")
    if mrr is not None:
        mrr = min(LEVER_RR_HI, max(LEVER_RR_LO, float(mrr)))
    st = cfg.get("allowed_strategies")
    if st is not None:
        st = sorted(set(str(x) for x in st) & STRATEGY_KEYS) or None
    rmin, rmax = cfg.get("risk_min"), cfg.get("risk_max")
    if rmin is not None:
        rmin = min(LEVER_RISK_HI, max(LEVER_RISK_LO, float(rmin)))
    if rmax is not None:
        rmax = min(LEVER_RISK_HI, max(LEVER_RISK_LO, float(rmax)))
    if rmin is not None and rmax is not None and rmin > rmax:
        rmin, rmax = rmax, rmin
    return {"id": cid, "entry_conf_floor": round(ecf, 2), "min_rr": mrr,
            "allowed_strategies": st, "risk_min": rmin, "risk_max": rmax,
            "born_ts": float(cfg.get("born_ts") or time.time()),
            "origin": "lever-sweep", "note": str(cfg.get("note", ""))[:120]}


def _sample_lever_config(run_idx, slot):
    """One mechanically-sampled point in the legal lever space, seeded from the
    run counter so a re-run of the same cycle nominates the same configs (no
    wall-clock in the seed — reproducibility is part of the discipline)."""
    rng = random.Random(run_idx * 1000003 + slot * 97 + 42)
    cfg = {"id": f"lab_r{run_idx}s{slot}",
           "entry_conf_floor": round(rng.uniform(LEVER_CONF_LO, LEVER_CONF_HI), 2),
           "min_rr": None if rng.random() < 0.34
                     else round(rng.uniform(LEVER_RR_LO, LEVER_RR_HI), 1),
           "born_ts": time.time(), "origin": "lever-sweep"}
    if rng.random() < 0.5:
        cfg["allowed_strategies"] = None
    else:
        cfg["allowed_strategies"] = sorted(rng.sample(sorted(STRATEGY_KEYS),
                                                      rng.randint(2, 4)))
    if rng.random() < 0.5:
        cfg["risk_min"] = cfg["risk_max"] = None
    else:
        r1 = round(rng.uniform(LEVER_RISK_LO, LEVER_RISK_HI), 2)
        r2 = round(rng.uniform(LEVER_RISK_LO, LEVER_RISK_HI), 2)
        cfg["risk_min"], cfg["risk_max"] = min(r1, r2), max(r1, r2)
    cfg["note"] = f"seeded lever sample run {run_idx} slot {slot}"
    return _sanitize_lever_config(cfg)


def _read_autopilot_scores():
    """Autopilot's persisted per-config scores (file only — the lab never opens
    a DB connection). Missing/corrupt file just means 'no evidence yet'."""
    try:
        with open(os.path.join(DATA_DIR, "autopilot_state.json"), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("scores"), dict):
            return d["scores"]
    except Exception:
        pass
    return {}


def _phase_nominations(state, run_idx):
    """Maintain the 3-slot challenger book: retire proven losers and timed-out
    slots, refill with fresh seeded samples. Retirement uses AUTOPILOT'S OWN
    paper evidence, not the lab's backtests — the slot's job was to earn OOS
    paper trades, and n>=40 with oos_edge<=0 means it demonstrably did not."""
    out = {"retired": [], "fresh": [], "kept": []}
    try:
        try:
            with open(CHALLENGERS_PATH, encoding="utf-8") as f:
                book = json.load(f)
            configs = book.get("configs", []) if isinstance(book, dict) else []
        except Exception:
            configs = []
        scores = _read_autopilot_scores()
        now = time.time()
        kept = []
        for raw in configs[:MAX_SLOTS]:
            cfg = _sanitize_lever_config(raw) if isinstance(raw, dict) else None
            if cfg is None:
                continue
            s = scores.get(cfg["id"], {})
            n_tr = int(s.get("n") or 0)
            oe = s.get("oos_edge")
            age_d = (now - cfg["born_ts"]) / 86400.0
            if n_tr >= RETIRE_MIN_N and oe is not None and oe <= 0:
                out["retired"].append((cfg["id"], f"proven loser: n={n_tr}, oos_edge<=0"))
            elif age_d > RETIRE_DAYS and not s.get("clears_cost"):
                out["retired"].append((cfg["id"], f"{age_d:.0f}d without clearing"))
            else:
                kept.append(cfg)
        fresh = []
        slot = 0
        while len(kept) + len(fresh) < MAX_SLOTS and slot < 24:
            cfg = _sample_lever_config(run_idx, slot)
            slot += 1
            if cfg and cfg["id"] not in {c["id"] for c in kept + fresh}:
                fresh.append(cfg)
        _atomic_json(CHALLENGERS_PATH,
                     {"version": 1, "updated_at": time.time(),
                      "configs": (kept + fresh)[:MAX_SLOTS]})
        out["kept"] = [c["id"] for c in kept]
        out["fresh"] = [c["id"] for c in fresh]
        for cid, why in out["retired"]:
            _log(f"retired challenger {cid}: {why}")
    except Exception as e:
        _log(f"nominations phase died: {e}", "ERR")
    return out


# ── Cycle driver ─────────────────────────────────────────────────────────────
def _digest(state, dur_s, bf, sw, nom):
    """One honest plain-text line for Telegram. '0 cleared' is the expected
    result and is reported exactly as plainly as a survivor would be."""
    cost = bs.ROUND_TRIP_COST_PCT * 100
    if sw["complete"]:
        sweep_txt = (f"tested {len(_GRID) * len(HORIZONS)} param-combos "
                     f"({len(_FAMILIES)} families, {len(HORIZONS)} horizons) "
                     f"across {sw['pairs_used']} pairs; "
                     f"{len(sw['survivors'])} cleared the OOS+cost bar "
                     f"(|t|>2 IS, |t|>{T_OOS_BAR} OOS, same sign, net of {cost:.2f}%)")
        if sw["survivors"]:
            sweep_txt += ". Survivors: " + ", ".join(sw["survivors"][:5])
    else:
        sweep_txt = f"sweep incomplete ({sw['note'] or 'budget'})"
    ch = ", ".join(nom.get("kept", []) + nom.get("fresh", [])) or "none"
    return (f"Research: backfilled {bf['tried']} pairs (+{bf['new_rows']:,} candles"
            + (f", {bf['failed']} failed" if bf["failed"] else "") + "); "
            + sweep_txt + f". Challengers: {ch}"
            + (f" ({len(nom['retired'])} retired)" if nom.get("retired") else "")
            + f". {dur_s/60:.1f}/{RESEARCH_BUDGET_MINS:.0f} budget mins used.")


def run_cycle(backfill_only=False):
    """One --cycle invocation: lock -> backfill -> sweep -> nominate -> ledger
    -> digest. Called nightly by bot_server's scheduler as a subprocess so a
    crash here can never take the trading loop with it."""
    if not _take_lock():
        return 0
    try:                                     # deprioritize below the live bot
        os.nice(19)
    except (AttributeError, OSError):
        pass                                 # Windows has no os.nice
    state = _load_state()
    state["running"] = True
    _save_state(state)
    t0 = time.time()
    deadline = time.monotonic() + RESEARCH_BUDGET_MINS * 60
    bf = {"tried": 0, "new_rows": 0, "failed": 0}
    sw = {"complete": False, "pairs_used": 0, "survivors": [], "note": "skipped"}
    nom = {"retired": [], "fresh": [], "kept": []}
    try:
        bf = _phase_backfill(state, deadline)
        if not backfill_only:
            sw = _phase_sweep(state, deadline)
            run_idx = int(state.get("run_counter", 0)) + 1
            nom = _phase_nominations(state, run_idx)
            state["run_counter"] = run_idx
            dur = time.time() - t0
            state["runs"] = (state.get("runs", []) + [{
                "ts": t0, "duration_s": round(dur, 1),
                "pairs_used": sw["pairs_used"],
                "families_tested": len(_FAMILIES) if sw["complete"] else 0,
                "params_tested": len(_GRID) * len(HORIZONS) if sw["complete"] else 0,
                "survivors": sw["survivors"],
                "note": sw["note"] or ("complete" if sw["complete"] else ""),
            }])[-RUNS_KEPT:]
            state["last_digest"] = _digest(state, dur, bf, sw, nom)
            try:                             # a dead Telegram never fails research
                bs.tg(state["last_digest"], plain=True)
            except Exception as e:
                _log(f"digest send failed: {e}", "WARN")
            _log(state["last_digest"])
        state["last_run_ts"] = time.time()
    except Exception as e:
        _log(f"cycle error: {e}", "ERR")
    finally:
        state["running"] = False
        _save_state(state)
        _release_lock()
    return 0


def print_status():
    """Human summary of the ledger — the terminal-side view of GET /research."""
    st = _load_state()
    res = st.get("family_results", {})
    cleared = [k for k, r in res.items() if r.get("clears")]
    print(f"research lab  enabled={RESEARCH_LAB_ENABLED}  "
          f"hour={RESEARCH_HOUR_UTC}Z  budget={RESEARCH_BUDGET_MINS:.0f}m")
    lr = st.get("last_run_ts", 0)
    print(f"last run: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(lr)) if lr else 'never'}"
          f"  running={st.get('running')}  runs={len(st.get('runs', []))}"
          f"  cursor={st.get('cursor')}")
    print(f"backfill ledger: {len(st.get('backfill', {}))} pairs tracked")
    print(f"family results: {len(res)} tests scored, {len(cleared)} cleared")
    top = sorted(res.items(), key=lambda kv: abs(kv[1].get("t_oos") or 0), reverse=True)[:8]
    for tid, r in top:
        print(f"  {tid:32s} t_is {r['t_is']:+6.2f}  t_oos {r['t_oos']:+6.2f}  "
              f"oos_edge {r['edge_oos']*100:+.3f}%  net {r['net_oos_edge_after_cost']*100:+.3f}%"
              f"  {'CLEARS' if r.get('clears') else ''}")
    try:
        with open(CHALLENGERS_PATH, encoding="utf-8") as f:
            book = json.load(f)
        print("challengers: " + (", ".join(c.get("id", "?") for c in book.get("configs", []))
                                 or "none"))
    except Exception:
        print("challengers: none (no file)")
    if st.get("last_digest"):
        print(f"last digest: {st['last_digest']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cycle", action="store_true", help="run one research cycle")
    ap.add_argument("--status", action="store_true", help="print ledger summary")
    ap.add_argument("--backfill-only", action="store_true", help="just top up history")
    args = ap.parse_args()
    if args.cycle:
        return run_cycle()
    if args.backfill_only:
        return run_cycle(backfill_only=True)
    if args.status:
        return print_status()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
