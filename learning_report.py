#!/usr/bin/env python3
"""Read what the learning lab has gathered: were the gates right, and would a
different exit have paid?

Run on the server, where the database lives:

    docker exec cryptobot-bot-1 python3 learning_report.py

Two questions, straight from the tables:

  SHADOW — for every signal that reached the gates, taken or rejected, what
  did price do next? A gate is earning its keep only if the signals it
  rejected went on to LOSE. "Blocked 500 signals" is not a defense; "blocked
  500 signals that averaged -0.4%" is.

  EXIT LAB — for every closed trade, the exit that happened vs holding a flat
  24h/48h from the same entry, both net of the same round-trip cost.

Read the n column before believing anything. The rsi_zone episode is the
house rule: a suggestive number on a small sample is a hypothesis for the
rig (time split, second timeframe, de-overlapped windows), not a change.
"""
import os
import statistics
import sys

import bot_server as bs


def fmt(v, pct=True):
    if v is None:
        return "     —"
    return f"{v*100:+6.3f}%" if pct else f"{v:+6.2f}"


def signed(sig, f):
    if f is None:
        return None
    return -f if sig == "SELL" else f


def main():
    if not bs.db.connected:
        print("no DATABASE_URL — run inside the bot container:")
        print("  docker exec cryptobot-bot-1 python3 learning_report.py")
        return 2
    cur = bs.db.conn.cursor()

    # ── shadow overview ──────────────────────────────────────────────────────
    cur.execute("""SELECT taken, rejected_by, sig, fwd6, fwd24, fwd48
                   FROM shadow_signals WHERE fwd_done=1""")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM shadow_signals")
    total, t0, t1 = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM shadow_signals WHERE fwd_done=0")
    pending = cur.fetchone()[0]

    print("=" * 72)
    print(f"  SHADOW BOOK — {total or 0} signals logged, "
          f"{len(rows)} with outcomes, {pending} awaiting their 48h")
    print("=" * 72)
    if rows:
        groups = {}
        for taken, rej, sig, f6, f24, f48 in rows:
            key = "TAKEN" if taken else (rej or "other")
            groups.setdefault(key, []).append(
                (signed(sig, f6), signed(sig, f24), signed(sig, f48)))
        print(f"  {'group':12s} {'n':>5s} {'fwd 6h':>8s} {'fwd 24h':>8s} {'fwd 48h':>8s}")
        print("  " + "-" * 46)
        for key in sorted(groups, key=lambda k: -len(groups[k])):
            g = groups[key]
            m = [statistics.fmean([x[i] for x in g if x[i] is not None])
                 if any(x[i] is not None for x in g) else None for i in range(3)]
            print(f"  {key:12s} {len(g):>5d} {fmt(m[0]):>8s} {fmt(m[1]):>8s} {fmt(m[2]):>8s}")
        print()
        print("  Reading it: positive means the signal direction was right.")
        print("  A rejecting gate is vindicated by NEGATIVE numbers on its row;")
        print("  if its row is positive, the gate is blocking winners.")
    else:
        print("  no outcomes yet — rows gain forward returns 49h after logging")

    # ── exit lab ─────────────────────────────────────────────────────────────
    cur.execute("""SELECT reason, act_gross, cost_pct, f24, f48
                   FROM exit_lab WHERE done=1""")
    er = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM exit_lab WHERE done=0")
    ep = cur.fetchone()[0]
    print()
    print("=" * 72)
    print(f"  EXIT LAB — {len(er)} trades with counterfactuals, {ep} awaiting")
    print("=" * 72)
    if er:
        act = [a - c for _, a, c, _, _ in er]
        h24 = [f - c for _, _, c, f, _ in er if f is not None]
        h48 = [f - c for _, _, c, _, f in er if f is not None]
        print(f"  {'strategy':22s} {'n':>5s} {'net/trade':>10s}")
        print("  " + "-" * 42)
        print(f"  {'actual exits':22s} {len(act):>5d} {fmt(statistics.fmean(act)):>10s}")
        if h24:
            print(f"  {'hold flat 24h':22s} {len(h24):>5d} {fmt(statistics.fmean(h24)):>10s}")
        if h48:
            print(f"  {'hold flat 48h':22s} {len(h48):>5d} {fmt(statistics.fmean(h48)):>10s}")
        by = {}
        for reason, a, c, _, _ in er:
            by.setdefault(reason or "?", []).append(a - c)
        print()
        print("  actual exits by reason:")
        for r, v in sorted(by.items(), key=lambda x: -len(x[1])):
            print(f"    {r:20s} n={len(v):>4d}  net {fmt(statistics.fmean(v))}")
    else:
        print("  no counterfactuals yet — they fill 49h after each close")

    print()
    print("  Small n = hypothesis, not finding. Promote nothing without the rig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
