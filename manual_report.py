#!/usr/bin/env python3
"""What can we honestly learn from Noluv's own (manual) trades?

The bot's entry signal was measured edgeless (t=+0.04 over 1,421 signals). The
obvious next question is whether the HUMAN trading the same market by hand is
doing better — and if so, which of the bot's gates he is profitably overriding.

This report answers that with the same discipline that killed 15 strategies,
and refuses to answer when the sample cannot support it.

THE TRAP THIS IS BUILT TO AVOID. Every manual trade so far is a LONG. Judging
long-only trades by "did they profit" scores the MARKET, not the trader — the
exact mistake that produced a spurious t = -4.19 earlier in this project. So
each trade is scored against a DIRECTION-MATCHED BASELINE: the distribution of
returns from every same-length, same-direction window on the same pair over the
surrounding period. "Better than a random moment to be long" is the only claim
that means anything, and the percentile against that distribution is what gets
reported.

Also reported, because they change the answer:
  - LEVERAGE-STRIPPED move. P&L on margin at 5x is 5x the price move; skill
    lives in the move, not the multiplier.
  - REAL COSTS. The book charged the old modeled fee; Kraken restructured on
    2026-07-09 and this account's tier is ~1.30% round trip, charged on
    NOTIONAL, so leverage multiplies it. Both are shown.
  - BUY-AND-HOLD of the same capital over the same span, which is what doing
    nothing would have paid.
  - THE BOT'S VIEW at entry, from shadow_signals: did the bot see a signal on
    that pair near that moment, and which gate rejected it? A gate the human
    profitably overrides is a gate worth re-examining — that is the one route
    by which this can genuinely improve the bot.

SAMPLE GATES ARE ENFORCED IN CODE, not documented and ignored. Below
MIN_N_INFER closed trades the report prints descriptive facts and explicitly
refuses to conclude anything. This matters more than usual here: the reader is
a beginner who may risk real money on what this says.

Usage (inside the bot container, where the DB and the book both live):
    docker exec cryptobot-bot-1 python3 manual_report.py
"""
import json
import os
import statistics
import sys
import time

MIN_N_INFER = 30          # below this: descriptive only, no inference
MIN_N_GATE  = 20          # per-gate override claims need at least this many
COST_MODELED = 0.0052     # what the book charged (maker-era model)
COST_REAL    = 0.0130     # Kraken Tier 1 since 2026-07-09, round trip
BASELINE_DAYS = 30        # window the random-entry distribution is drawn from

DATA_DIR = os.environ.get("DATA_DIR", "/data")


def load_book():
    path = os.path.join(DATA_DIR, "manual_book.json")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "manual_book.json")
    with open(path) as f:
        return json.load(f)


def db_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        print(f"(no DB: {e})")
        return None


def candles(conn, pair, t0, t1, interval=60):
    """[(ts, close)] ascending for pair within [t0, t1]."""
    if not conn:
        return []
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, close FROM candles
                       WHERE pair=%s AND interval_m=%s AND ts BETWEEN %s AND %s
                       ORDER BY ts""", (pair, interval, t0, t1))
        return [(float(r[0]), float(r[1])) for r in cur.fetchall()]


def baseline_dist(series, hold_s, side):
    """Direction-matched baseline: return of EVERY same-length window in the
    series, signed the same way as the trade. This is 'what a random moment to
    be long was worth', which is what a long-only trade must beat to mean
    anything."""
    out = []
    if len(series) < 3:
        return out
    step = 3600
    n_bars = max(1, int(round(hold_s / step)))
    sgn = 1 if side.upper() in ("BUY", "LONG") else -1
    for i in range(0, len(series) - n_bars):
        a, b = series[i][1], series[i + n_bars][1]
        if a > 0:
            out.append(sgn * (b - a) / a)
    return out


def pct_rank(x, dist):
    if not dist:
        return None
    return 100.0 * sum(1 for d in dist if d < x) / len(dist)


def bot_view(conn, pair, t_entry, window_s=7200):
    """What the bot thought about this pair near the entry: nearest shadow
    signals within +/- window_s, and which gate rejected them."""
    if not conn:
        return None
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, sig, conf, rejected_by FROM shadow_signals
                       WHERE pair=%s AND ts BETWEEN %s AND %s
                       ORDER BY abs(ts - %s) LIMIT 5""",
                    (pair, t_entry - window_s, t_entry + window_s, t_entry))
        return cur.fetchall()


def main():
    book = load_book()
    trades = book.get("trades", [])
    conn = db_conn()
    n = len(trades)

    print("=" * 74)
    print("WHAT YOUR OWN TRADES SHOW".center(74))
    print("=" * 74)
    print(f"\nclosed manual trades: {n}    balance: ${book.get('balance', 0):,.2f}"
          f"    open: {len(book.get('positions', {}))}")
    if not trades:
        print("\nNo closed manual trades yet — nothing to measure.")
        return 0

    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    longs = sum(1 for t in trades if t.get("side", "").upper() == "BUY")
    pnl_total = sum(t.get("pnl", 0) for t in trades)
    print(f"wins: {wins}/{n}    longs: {longs}/{n}    total P&L: ${pnl_total:+,.2f}")

    if longs == n and n > 0:
        print("\n  !! EVERY trade is a LONG. 'Did it profit' therefore measures the")
        print("     MARKET, not you. Each trade is scored below against the return")
        print("     of a RANDOM moment to be long the same coin for the same time.")

    print("\n" + "-" * 74)
    print("PER TRADE — scored against a random entry of the same length")
    print("-" * 74)

    ranks, alphas = [], []
    for t in trades:
        pair = t.get("pair", "?")
        side = t.get("side", "BUY")
        lev  = float(t.get("leverage", 1) or 1)
        ts_exit = float(t.get("ts", 0))
        hold_s  = float(t.get("held_mins", 0)) * 60
        ts_entry = ts_exit - hold_s
        entry, exitp = float(t.get("entry", 0)), float(t.get("exit", 0))
        sgn = 1 if side.upper() == "BUY" else -1
        move = sgn * (exitp - entry) / entry if entry else 0.0

        series = candles(conn, pair, ts_entry - BASELINE_DAYS * 86400, ts_exit)
        dist = baseline_dist(series, hold_s, side)
        rank = pct_rank(move, dist)
        med  = statistics.median(dist) if dist else None
        alpha = (move - med) if med is not None else None
        if rank is not None:
            ranks.append(rank)
        if alpha is not None:
            alphas.append(alpha)

        # costs scale with leverage because fees are charged on notional
        net_modeled = move - COST_MODELED * lev
        net_real    = move - COST_REAL * lev

        print(f"\n  {pair} {side} {lev:g}x   held {hold_s/86400:.1f}d   "
              f"{time.strftime('%m-%d %H:%M', time.gmtime(ts_entry))} → "
              f"{time.strftime('%m-%d %H:%M', time.gmtime(ts_exit))}")
        print(f"    price move (leverage stripped) : {move*100:+7.2f}%")
        print(f"    booked P&L (on margin, {lev:g}x)   : ${t.get('pnl', 0):+,.2f}")
        if med is not None:
            print(f"    random same-length long        : median {med*100:+7.2f}%"
                  f"   → you were better than {rank:.0f}% of random moments")
            print(f"    edge over a random moment      : {alpha*100:+7.2f}%"
                  f"   {'(timing helped)' if alpha > 0 else '(the market did the work)'}")
        else:
            print("    (no candle history for a baseline on this window)")
        print(f"    net of costs  modeled {COST_MODELED*lev*100:.2f}% / real "
              f"{COST_REAL*lev*100:.2f}%  →  {net_modeled*100:+.2f}% / {net_real*100:+.2f}%")

        bv = bot_view(conn, pair, ts_entry)
        if bv:
            gates = {}
            for _ts, _sig, _conf, rej in bv:
                gates[rej or "TAKEN"] = gates.get(rej or "TAKEN", 0) + 1
            print(f"    the bot at that moment         : {dict(gates)}")
        elif conn:
            print("    the bot at that moment         : no signal logged on this pair")

    # ── independence: overlapping trades are not separate evidence ──────────
    # Overlap inflated a t-stat ~3x elsewhere in this project (gap_fade: +6.14
    # overlapping → +1.73 non-overlapping). Concurrent long positions in
    # correlated coins are ONE bet on the market wearing several hats, so the
    # effective sample is the number of disjoint calendar spans, not len(trades).
    spans = sorted((float(t["ts"]) - float(t.get("held_mins", 0)) * 60,
                    float(t["ts"])) for t in trades)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = sum(e - s for s, e in merged)
    span_all = spans[-1][1] - spans[0][0] if spans else 0
    max_conc = 0
    for s, e in spans:
        conc = sum(1 for s2, e2 in spans if s2 < e and e2 > s)
        max_conc = max(max_conc, conc)

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)

    if ranks:
        print(f"\n  average percentile vs random entries: {statistics.fmean(ranks):.0f}%"
              f"   (50% = pure luck, no timing skill)")
    if alphas:
        m = statistics.fmean(alphas)
        print(f"  average edge over a random moment   : {m*100:+.2f}% per trade")

    print(f"\n  INDEPENDENCE CHECK — the number above is inflated if trades overlap:")
    print(f"    {n} trades occupy {len(merged)} disjoint time span(s) over "
          f"{span_all/86400:.0f} calendar days")
    print(f"    up to {max_conc} position(s) open at once; "
          f"{covered/86400:.0f} days of the period held exposure")
    if len(merged) < n:
        print(f"    → these are NOT {n} independent bets. Overlapping longs in "
              f"correlated coins")
        print(f"      are ONE bet on the market, counted {n} times. "
              f"Effective sample ≈ {len(merged)}.")

    if n < MIN_N_INFER:
        print(f"\n  ⚠ {n} trades is NOT enough to conclude anything.")
        print(f"    {wins}/{n} wins happens by pure chance {100*0.5**n:.1f}% of the time")
        print(f"    at a 50/50 coin flip. Statistical inference starts at "
              f"~{MIN_N_INFER} trades;")
        print(f"    a claim about a specific gate needs ~{MIN_N_GATE} trades that "
              f"overrode THAT gate.")
        print("\n    What the numbers above ARE: a description of what happened.")
        print("    What they are NOT: evidence that this approach makes money.")
        print("    Nothing here justifies risking real money.")
    else:
        print(f"\n  n={n} — inference enabled. See per-gate breakdown above.")

    print("\n  Every future manual trade is now recorded with the bot's full view")
    print("  at that moment, so this report gets sharper as the sample grows.\n")
    if conn:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
