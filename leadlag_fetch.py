#!/usr/bin/env python3
"""Build a minute-level two-venue dataset for the cross-exchange lead-lag test.

The last untested door: every PRICE-based signal failed the cost hurdle
(find_signal.py, 1.4M candles), so the only remaining edge source is
information the Kraken chart doesn't contain. Cheapest candidate: does the
bigger venue's price (Coinbase) lead Kraken by seconds-to-minutes?

Two feeds per pair, both 1-minute, both UTC minute-start timestamps:

  Coinbase: /products/{X}-USD/candles?granularity=60 — real 1m candles,
      300 per request, walked forward.  data/leadlag/{PAIR}_cb_1.csv
      (ts,open,high,low,close,volume)

  Kraken: REST OHLC only serves the last 720 minutes, so minute candles are
      REBUILT from the raw public Trades feed (full history, ~1000 trades per
      call, nanosecond cursor pagination).  data/leadlag/{PAIR}_kr_1.csv
      (ts,open,high,low,close,volume,ntrades,last_trade_ts)
      ntrades + last_trade_ts are kept ON PURPOSE: the staleness guard in
      leadlag_test.py needs them to kill the classic lead-lag mirage where the
      "edge" is just Kraken's stale last print catching up — predictable on
      paper, untradeable in reality.

Resumable: Coinbase resumes from the file tail; Kraken persists a JSON
checkpoint (API cursor + the partial, un-flushed minute's trades) after every
batch, so a kill loses nothing. Stdlib only — runs on the no-AVX server.

Usage:
    python3 leadlag_fetch.py --days 60           # full build (hours: Kraken)
    python3 leadlag_fetch.py --days 0.05 --pairs XBTUSD   # smoke test
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

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "leadlag")
CB = "https://api.exchange.coinbase.com"
KR = "https://api.kraken.com/0/public/Trades"
SPECIAL = {"XBT": "BTC", "XDG": "DOGE"}

# Liquidity tiers on purpose: if lead-lag exists it should be strongest where
# Kraken is thinnest, and weakest on BTC. Testing only majors would hide that.
PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD",
         "XDGUSD", "LINKUSD", "SHIBUSD", "PEPEUSD"]


def coin(pair):
    return SPECIAL.get(pair[:-3], pair[:-3])


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-research"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def last_ts(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        try:
            f.seek(-300, os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read().decode(errors="replace").strip().splitlines()
    for line in reversed(tail):
        bits = line.split(",")
        if bits and bits[0].isdigit():
            return int(bits[0])
    return None


# ── Coinbase: real 1m candles, 300 per request ──────────────────────────────

def pull_coinbase(pair, t0, now):
    product = f"{coin(pair)}-USD"
    try:
        get(f"{CB}/products/{product}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  {pair:8s} cb: no such product", flush=True)
            return 0
        raise
    path = os.path.join(OUT, f"{pair}_cb_1.csv")
    resume = last_ts(path)
    start = (resume + 60) if resume else t0
    mode = "a" if resume else "w"
    rows = 0
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
        while start < now - 60:
            end = min(start + 300 * 60, now)
            q = urllib.parse.urlencode({
                "granularity": 60,
                "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                "end":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
            })
            for attempt in range(6):
                try:
                    batch = get(f"{CB}/products/{product}/candles?{q}")
                    break
                except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                    code = getattr(e, "code", None)
                    if code == 429 or code is None or code >= 500:
                        time.sleep(2 + attempt * 3)
                        continue
                    raise
            else:
                batch = []
            # newest-first [time, low, high, open, close, volume] -> ascending
            for c in sorted(batch or [], key=lambda x: x[0]):
                t = int(c[0])
                if t <= (resume or 0) or t + 60 > now:      # dupes + forming bar
                    continue
                w.writerow([t, c[3], c[2], c[1], c[4], c[5]])
                resume = t
                rows += 1
            start = end
            time.sleep(0.3)
    return rows


# ── Kraken: minute candles rebuilt from the raw trades feed ─────────────────

def _flush(w, buckets, upto):
    """Write every completed minute strictly before `upto`, return count."""
    n = 0
    for m in sorted(k for k in buckets if k < upto):
        tr = buckets.pop(m)
        prices = [p for p, _, _ in tr]
        w.writerow([m, tr[0][0], max(prices), min(prices), tr[-1][0],
                    round(sum(v for _, v, _ in tr), 8), len(tr),
                    round(tr[-1][2], 3)])
        n += 1
    return n


def pull_kraken(pair, t0, now):
    path = os.path.join(OUT, f"{pair}_kr_1.csv")
    ckpt = path + ".ckpt"
    buckets = {}                       # minute_start -> [(price, vol, trade_ts)]
    if os.path.exists(ckpt):
        with open(ckpt, encoding="utf-8") as f:
            state = json.load(f)
        cursor = state["cursor"]
        for p, v, t in state.get("partial", []):
            buckets.setdefault(int(t // 60) * 60, []).append((p, v, t))
        mode = "a"
    else:
        cursor = str(int(t0 * 1e9))
        mode = "w"
    f = open(path, mode, newline="", encoding="utf-8")
    w = csv.writer(f)
    if mode == "w":
        w.writerow(["ts", "open", "high", "low", "close", "volume",
                    "ntrades", "last_trade_ts"])
    rows = calls = 0
    t_wall = time.time()
    while True:
        url = f"{KR}?pair={pair}&since={cursor}&count=1000"
        for attempt in range(8):
            try:
                resp = get(url)
                if resp.get("error"):
                    err = ",".join(resp["error"])
                    if "Rate limit" in err or "Too many" in err:
                        time.sleep(10 + attempt * 5)
                        continue
                    raise RuntimeError(err)
                break
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                time.sleep(5 + attempt * 5)
        else:
            print(f"  {pair:8s} kr: giving up after retries at {cursor}", flush=True)
            break
        res = resp["result"]
        cursor = res.pop("last")
        trades = next(iter(res.values()), [])
        # No t >= now skip here: `now` is frozen at main() start, but this
        # grind runs for minutes-to-hours, so that guard silently dropped
        # trades the cursor had already advanced past — a permanent hole (and
        # one censored candle) per pair on any resumed run. The flush guards
        # below are what keep forming minutes out of the file.
        for tr in trades:
            p, v, t = float(tr[0]), float(tr[1]), float(tr[2])
            buckets.setdefault(int(t // 60) * 60, []).append((p, v, t))
        newest = float(trades[-1][2]) if trades else now
        # flush all minutes certainly complete, keep the newest (partial) one
        rows += _flush(w, buckets, int(newest // 60) * 60)
        f.flush()
        with open(ckpt, "w", encoding="utf-8") as cf:
            json.dump({"cursor": cursor,
                       "partial": [t for b in buckets.values() for t in b]}, cf)
        calls += 1
        if calls % 200 == 0:
            done = max(newest - t0, 1) / max(now - t0, 1)
            rate = (time.time() - t_wall) / calls
            eta = (1 - done) / max(done, 1e-9) * (time.time() - t_wall)
            print(f"  {pair:8s} kr: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(newest))}"
                  f" ({done*100:4.1f}%, {rate:.2f}s/call, eta {eta/3600:.1f}h)",
                  flush=True)
        if len(trades) < 1000 and newest >= time.time() - 120:   # caught up
            # fresh wall clock, not the frozen `now`: flush every minute that
            # is certainly complete, never the still-forming one
            rows += _flush(w, buckets, int((time.time() - 60) // 60) * 60)
            break
        time.sleep(1.1)                 # public rate limit: ~1 call/sec
    f.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=60)
    ap.add_argument("--pairs", default="")
    ap.add_argument("--skip-kraken", action="store_true")
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()] or PAIRS
    os.makedirs(OUT, exist_ok=True)
    now = int(time.time())
    t0 = now - int(args.days * 86400)

    # Coinbase first: minutes per pair. Kraken after: the overnight grind.
    for p in pairs:
        tw = time.time()
        n = pull_coinbase(p, t0, now)
        print(f"  {p:8s} cb: +{n:>7,} candles ({time.time()-tw:.0f}s)", flush=True)
    if not args.skip_kraken:
        for p in pairs:
            tw = time.time()
            n = pull_kraken(p, t0, now)
            print(f"  {p:8s} kr: +{n:>7,} candles ({(time.time()-tw)/60:.1f}m)",
                  flush=True)
    print("ALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
