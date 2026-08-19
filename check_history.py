#!/usr/bin/env python3
"""Integrity scan for data/history — trust the data before trusting any result.

Every research conclusion downstream rests on these files, and they came from
a third exchange via a fetcher that raced a smoke test on its first run. So:

  per pair: row count · date span · strictly-increasing timestamps ·
            duplicate timestamps · hour gaps · zero/negative prices ·
            high<low bars · last close vs LIVE Kraken price (proves the
            symbol mapping — a wrong mapping is silent poison)

--fix rewrites files sorted + de-duplicated (keeps the last row per ts).

Usage:
    python check_history.py
    python check_history.py --fix
"""
import argparse
import csv
import os
import sys
import time

import bot_server as bs

bs.log = lambda *a, **k: None
HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history")


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for bits in r:
            if len(bits) < 6 or not bits[0].isdigit():
                continue
            try:
                rows.append((int(bits[0]), float(bits[1]), float(bits[2]),
                             float(bits[3]), float(bits[4]), float(bits[5])))
            except ValueError:
                continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(HIST) if f.endswith("_60.csv"))
    print(f"{len(files)} history files in {HIST}\n")
    print(f"{'pair':9s} {'rows':>8s} {'from':>11s} {'to':>11s} {'dup':>5s} "
          f"{'gaps':>6s} {'bad':>4s} {'vs Kraken':>10s}")
    print("-" * 66)
    problems = 0
    total = 0
    for fn in files:
        pair = fn.split("_")[0]
        rows = load(os.path.join(HIST, fn))
        if not rows:
            print(f"{pair:9s} {'EMPTY':>8s}")
            problems += 1
            continue
        total += len(rows)
        ts = [r[0] for r in rows]
        dups = len(ts) - len(set(ts))
        srt = sorted(set(ts))
        gaps = sum(1 for a, b in zip(srt, srt[1:]) if b - a > 3600)
        bad = sum(1 for r in rows if r[4] <= 0 or r[2] < r[3])
        span_f = time.strftime("%Y-%m-%d", time.gmtime(srt[0]))
        span_t = time.strftime("%Y-%m-%d", time.gmtime(srt[-1]))
        # live cross-check: same asset on Kraken should be within a few percent
        try:
            live = bs.get_price(pair)
            drift = abs(rows[-1][4] - live) / live * 100
            vs = f"{drift:8.2f}%"
            if drift > 5:
                vs += " ⚠"
                problems += 1
        except Exception:
            vs = "       n/a"
        flag = ""
        if dups or bad:
            problems += 1
            flag = "  ⚠"
        print(f"{pair:9s} {len(rows):>8,} {span_f:>11s} {span_t:>11s} {dups:>5d} "
              f"{gaps:>6d} {bad:>4d} {vs}{flag}")

        if args.fix and (dups or ts != srt):
            best = {}
            for r in rows:
                best[r[0]] = r                      # last write wins
            fixed = [best[t] for t in sorted(best)]
            with open(os.path.join(HIST, fn), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "open", "high", "low", "close", "volume"])
                w.writerows(fixed)
            print(f"{'':9s} fixed -> {len(fixed):,} rows (sorted, de-duplicated)")

    print("-" * 66)
    print(f"total {total:,} candles · "
          + ("ALL CLEAN" if problems == 0 else f"{problems} file(s) need attention"))
    print("\nNote: gaps are real exchange downtime/delistings as well as fetch")
    print("misses — a few dozen over years is normal, thousands is not.")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
