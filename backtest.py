"""
backtest.py — Walk-forward backtester for CryptoBot.

It imports bot_server and reuses the EXACT live SignalEngine + PaperTrader,
so what you test is what you run. No reimplemented strategy to drift out of sync.

Two data sources:
  • Kraken public OHLC (free, but capped at ~720 candles per interval)
  • Your own CSV of candles (unlimited history) with columns:
        time,open,high,low,close,volume
    (extra columns ignored; header optional if --no-header)

Usage:
  python3 backtest.py --pair XBTUSD --interval 60
  python3 backtest.py --pair ETHUSD --interval 240
  python3 backtest.py --csv my_btc_5min.csv --pair XBTUSD

Honest limitations (read these):
  • Candle-close granularity: exits fire at candle close, so intrabar wick
    hits on stops/targets are not modelled. Results are approximate.
  • Macro gates (news, NASDAQ, Fear&Greed, BTC dominance, funding) are
    NEUTRAL in backtest — we have no historical values for those feeds.
    So this tests the TECHNICAL core (EMA/RSI/MACD/volume/ATR/trailing).
  • The time-of-day filter and account "elimination"/respawn are disabled
    so you measure the raw strategy, not the gamification.
  • Free Kraken history is short. For a real multi-month test, feed a CSV.
"""

import argparse
import csv
import os
import sys

# Import the live bot. Env vars are optional now (import-safe).
import bot_server as bs


# ── Make the imported live code backtest-safe ──────────────────────────────────
bs._in_active_hours = lambda: True          # don't gate on wall-clock time
bs._print_trade_box = lambda *a, **k: None  # silence per-trade terminal boxes
bs._print_rank_table = lambda *a, **k: None


def fetch_kraken(pair, interval):
    """Most recent ~720 candles from Kraken public OHLC."""
    import requests
    r = requests.get(f"{bs.BASE_URL}/OHLC",
                     params={"pair": pair, "interval": interval}, timeout=15)
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken OHLC error: {payload['error']}")
    rkey = next(k for k in payload["result"] if k != "last")
    rows = payload["result"][rkey]
    # [time, open, high, low, close, vwap, volume, count]
    return [{"t": float(c[0]), "o": float(c[1]), "h": float(c[2]),
             "l": float(c[3]), "c": float(c[4]), "v": float(c[6])} for c in rows]


def load_csv(path):
    """Load candles from a CSV with time,open,high,low,close,volume."""
    out = []
    with open(path, newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample)
        reader = csv.reader(f)
        if has_header:
            next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            try:
                out.append({"t": float(row[0]), "o": float(row[1]),
                            "h": float(row[2]), "l": float(row[3]),
                            "c": float(row[4]), "v": float(row[5])})
            except ValueError:
                continue
    return out


def run(candles, pair, name):
    n = len(candles)
    warmup = bs.CANDLE_LIMIT
    if n <= warmup + 5:
        print(f"Not enough candles ({n}); need > {warmup + 5}. "
              f"Use a smaller --interval or a CSV with more history.")
        return None

    trader = bs.PaperTrader(no_persist=True)   # fresh, never touches DB/file
    trader._eliminate = lambda: None           # disable respawn; we break instead
    engine = bs.SignalEngine()

    equity = []      # realized balance sampled each step (for drawdown)
    last_sig = None
    blew_up = False

    for i in range(warmup, n):
        window = candles[i - warmup:i]
        closes = [c["c"] for c in window]
        highs  = [c["h"] for c in window]
        lows   = [c["l"] for c in window]
        vols   = [c["v"] for c in window]
        price  = closes[-1]

        try:
            atr = bs.calc_atr(highs, lows, closes)
        except Exception:
            atr = None

        # 1) manage any open position at this candle's price
        trader.on_signal("HOLD", price, 0, 0, name, 0.0, pair, atr=atr)

        # 2) evaluate a fresh signal on the current window
        try:
            sig, plan, ema, rsi, conf = engine.evaluate(
                closes, highs, lows, vols, price, [], pair=pair)
        except Exception:
            sig, plan = "HOLD", {}

        # 3) act only on a signal change (mirrors the live loop)
        if sig in ("BUY", "SELL") and sig != last_sig:
            stop   = plan.get("stop",   price * 0.985 if sig == "BUY" else price * 1.015)
            target = plan.get("exit",   price * 1.015 if sig == "BUY" else price * 0.985)
            fkey   = plan.get("fkey",   "")
            trader.on_signal(sig, price, stop, target, name, conf, pair, atr=atr, fkey=fkey)
        last_sig = sig

        equity.append(trader.balance)

        if trader.balance < bs.PAPER_FLOOR:
            blew_up = True
            break
        if trader.balance >= bs.PAPER_TARGET:
            break

    return summarize(trader, equity, blew_up, candles, name)


def summarize(trader, equity, blew_up, candles, name):
    trades = trader.trades
    start  = bs.PAPER_START
    end    = trader.balance
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # max drawdown from the realized equity curve
    peak = start
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_w = (gross_win / len(wins)) if wins else 0.0
    avg_l = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (sum(pnls) / len(trades)) if trades else 0.0

    span_days = (candles[-1]["t"] - candles[0]["t"]) / 86400 if len(candles) > 1 else 0

    line = "─" * 52
    print(f"\n{line}")
    print(f"  BACKTEST — {name}   ({span_days:.1f} days, {len(candles)} candles)")
    print(line)
    print(f"  Start balance      ${start:,.2f}")
    print(f"  End balance        ${end:,.2f}")
    print(f"  Total return       {(end/start - 1)*100:+.2f}%")
    print(f"  Trades             {len(trades)}")
    print(f"  Win rate           {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit factor      {pf:.2f}")
    print(f"  Avg win / loss     ${avg_w:.2f} / ${avg_l:.2f}")
    print(f"  Expectancy/trade   ${expectancy:+.3f}")
    print(f"  Max drawdown       {max_dd*100:.1f}%")
    if trades:
        print(f"  Best / worst       ${max(pnls):+.2f} / ${min(pnls):+.2f}")
    if blew_up:
        print(f"  ⚠️  ACCOUNT HIT FLOOR (${bs.PAPER_FLOOR:.0f}) — strategy blew up.")
    print(line)

    # write an equity curve for your dashboard / further analysis
    out_csv = f"equity_{name}.csv"
    try:
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "balance"])
            for idx, e in enumerate(equity):
                w.writerow([idx, round(e, 4)])
        print(f"  Equity curve → {out_csv}")
    except Exception as e:
        print(f"  (could not write equity curve: {e})")

    return {"return_pct": (end/start - 1)*100, "trades": len(trades),
            "win_rate": wr, "profit_factor": pf, "max_dd": max_dd*100,
            "blew_up": blew_up}


def main():
    ap = argparse.ArgumentParser(description="Backtest CryptoBot's live strategy.")
    ap.add_argument("--pair", default="XBTUSD", help="Kraken pair, e.g. XBTUSD, ETHUSD")
    ap.add_argument("--interval", type=int, default=60,
                    help="Candle minutes: 1,5,15,30,60,240,1440 (default 60)")
    ap.add_argument("--csv", help="Load candles from CSV instead of Kraken")
    args = ap.parse_args()

    name = args.pair
    if args.csv:
        if not os.path.exists(args.csv):
            print(f"CSV not found: {args.csv}")
            sys.exit(1)
        print(f"Loading candles from {args.csv} ...")
        candles = load_csv(args.csv)
    else:
        print(f"Fetching {args.pair} @ {args.interval}m from Kraken ...")
        candles = fetch_kraken(args.pair, args.interval)

    print(f"Loaded {len(candles)} candles.")
    run(candles, args.pair, name)


if __name__ == "__main__":
    main()
