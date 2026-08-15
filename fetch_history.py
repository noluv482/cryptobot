#!/usr/bin/env python3
"""Fetch multi-year 1h OHLCV history for the bot's whole universe.

Kraken's REST OHLC serves only the last 720 candles, which is why all research
so far lived inside one 30-day regime — the blind spot that let rsi_extreme
look real. Binance.com would be ideal but geo-blocks US IPs (HTTP 451).

Primary source: Coinbase Exchange public candles — US-legal, no auth, full
history, real volume. 300 candles per request, walked forward in windows.
NOTE the field order: [time, low, high, open, close, volume].
Fallback for pairs Coinbase lacks: Binance.US (thin volume, but usable prices).

Files land in data/history/{KRAKEN_PAIR}_60.csv (ts,open,high,low,close,volume)
under the KRAKEN pair name so every rig script can use them unchanged.
Re-runnable: existing files resume from their last timestamp.

Usage:
    python fetch_history.py                 # all 26 pairs
    python fetch_history.py --pairs SOLUSD,ETHUSD
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import bot_server as bs

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history")
CB = "https://api.exchange.coinbase.com"
BUS = "https://api.binance.us/api/v3/klines"
SPECIAL = {"XBT": "BTC", "XDG": "DOGE"}
GENESIS = 1483228800            # 2017-01-01; earlier is pre-history for this universe
WINDOW = 300 * 3600             # Coinbase max: 300 candles per request


def coin(pair):
    return SPECIAL.get(pair[:-3], pair[:-3])


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-research",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def last_ts(path):
    if not os.path.exists(path):
        return None
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
    return None


def pull_coinbase(pair, writer, resume):
    """Walk forward in 300h windows. Returns rows written, or None if the
    product does not exist on Coinbase."""
    product = f"{coin(pair)}-USD"
    try:
        get(f"{CB}/products/{product}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    start = (resume + 3600) if resume else GENESIS
    now = int(time.time())
    rows = 0
    empty_streak = 0
    while start < now:
        end = min(start + WINDOW, now)
        q = urllib.parse.urlencode({
            "granularity": 3600,
            "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            "end":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
        })
        for attempt in range(5):
            try:
                batch = get(f"{CB}/products/{product}/candles?{q}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + attempt * 2)
                    continue
                raise
        else:
            batch = []
        if batch:
            empty_streak = 0
            # newest-first [time, low, high, open, close, volume] -> ascending OHLCV
            for c in sorted(batch, key=lambda x: x[0]):
                t = int(c[0])
                if t <= (resume or 0) or t + 3600 > now:   # skip dupes + forming bar
                    continue
                writer.writerow([t, c[3], c[2], c[1], c[4], c[5]])
                resume = t
                rows += 1
        else:
            empty_streak += 1
        start = end
        time.sleep(0.25)                    # ~4 req/s, well inside public limits
    return rows


def pull_binanceus(pair, writer, resume):
    sym = f"{coin(pair)}USD"
    start_ms = ((resume + 3600) * 1000) if resume else GENESIS * 1000
    rows = 0
    while True:
        q = urllib.parse.urlencode({"symbol": sym, "interval": "1h",
                                    "startTime": start_ms, "limit": 1000})
        try:
            batch = get(f"{BUS}?{q}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None if rows == 0 else rows
            if e.code == 429:
                time.sleep(30)
                continue
            raise
        if not isinstance(batch, list) or not batch:
            break
        for c in batch:
            if c[6] > time.time() * 1000:
                continue
            writer.writerow([int(c[0] // 1000), c[1], c[2], c[3], c[4], c[5]])
            rows += 1
        start_ms = batch[-1][0] + 3600_000
        if len(batch) < 1000:
            break
        time.sleep(0.2)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="")
    args = ap.parse_args()
    pairs = ([p.strip() for p in args.pairs.split(",") if p.strip()]
             or [c["pair"] for c in bs.SCAN_UNIVERSE])
    os.makedirs(OUT, exist_ok=True)
    total = 0
    for p in pairs:
        t0 = time.time()
        path = os.path.join(OUT, f"{p}_60.csv")
        resume = last_ts(path)
        mode = "a" if resume else "w"
        try:
            with open(path, mode, newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if mode == "w":
                    w.writerow(["ts", "open", "high", "low", "close", "volume"])
                n = pull_coinbase(p, w, resume)
                src = "coinbase"
                if n is None:
                    n = pull_binanceus(p, w, resume)
                    src = "binance.us"
                if n is None:
                    n, src = 0, "NO SOURCE"
        except Exception as e:
            print(f"  {p:9s} FAILED: {e}", flush=True)
            continue
        if n == 0 and mode == "w" and src == "NO SOURCE":
            os.remove(path)
        total += n
        print(f"  {p:9s} +{n:>7,} candles  ({src}, {time.time()-t0:.0f}s)", flush=True)
    print(f"done — {total:,} new candles in {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
