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


def run(candles, pair, name, verbose=True):
    """Run a single backtest pass. Returns summary dict."""
    n = len(candles)
    warmup = bs.CANDLE_LIMIT
    if n <= warmup + 5:
        if verbose:
            print(f"Not enough candles ({n}); need > {warmup + 5}. "
                  f"Use a smaller --interval or a CSV with more history.")
        return {"return_pct": 0, "trades": 0, "win_rate": 0,
                "profit_factor": 0, "max_dd": 0, "blew_up": False}

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

    return summarize(trader, equity, blew_up, candles, name, verbose=verbose)


def summarize(trader, equity, blew_up, candles, name, verbose=True):
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
    if verbose:
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


def walk_forward(candles, pair, interval_mins, window_days=30, step_days=15):
    """Run walk-forward analysis: slide a 30-day window across the full dataset in 15-day steps.
    Reports edge stability — does the strategy stay profitable across different time periods?"""
    secs_per_candle = interval_mins * 60
    window_candles  = int(window_days  * 86400 / secs_per_candle)
    step_candles    = int(step_days   * 86400 / secs_per_candle)
    min_candles     = bs.CANDLE_LIMIT + 6   # must exceed run()'s warmup guard (CANDLE_LIMIT+5)

    if len(candles) < window_candles + step_candles:
        print(f"Not enough candles for walk-forward (need ≥ {window_candles + step_candles}, got {len(candles)})")
        return

    line = "─" * 64
    print(f"\n{line}")
    print(f"  WALK-FORWARD — {pair}  window={window_days}d  step={step_days}d")
    print(line)
    print(f"  {'Window':26s}  {'Return':>8s}  {'WR':>6s}  {'Trades':>7s}  {'PF':>5s}  {'MaxDD':>7s}")
    print(line)

    results = []
    idx = 0
    while idx + window_candles <= len(candles):
        window = candles[idx: idx + window_candles]
        if len(window) < min_candles:
            idx += step_candles
            continue
        from datetime import datetime as _dt
        t0 = _dt.utcfromtimestamp(window[0]["t"]).strftime("%Y-%m-%d")
        t1 = _dt.utcfromtimestamp(window[-1]["t"]).strftime("%Y-%m-%d")
        label = f"{t0} → {t1}"
        res = run(window, pair, label, verbose=False)
        sign = "+" if res["return_pct"] >= 0 else ""
        flag = " ✗" if res["blew_up"] else (" ✓" if res["return_pct"] >= 0 else "")
        print(f"  {label:26s}  {sign}{res['return_pct']:>6.1f}%  {res['win_rate']:>5.1f}%"
              f"  {res['trades']:>7d}  {res['profit_factor']:>5.2f}  {res['max_dd']:>6.1f}%{flag}")
        results.append(res)
        idx += step_candles

    if not results:
        print("  No windows completed.")
        return

    profitable = sum(1 for r in results if r["return_pct"] > 0 and not r["blew_up"])
    avg_ret   = sum(r["return_pct"] for r in results) / len(results)
    avg_wr    = sum(r["win_rate"]   for r in results) / len(results)
    avg_dd    = sum(r["max_dd"]     for r in results) / len(results)
    print(line)
    print(f"  Windows: {len(results)}  Profitable: {profitable}/{len(results)}"
          f"  Avg return: {avg_ret:+.1f}%  Avg WR: {avg_wr:.1f}%  Avg MaxDD: {avg_dd:.1f}%")
    if profitable / max(len(results), 1) < 0.5:
        print("  ⚠️  Edge is NOT stable — fewer than 50% of windows profitable.")
    elif profitable / max(len(results), 1) < 0.7:
        print("  ⚠️  Edge is MARGINAL — review gate settings before going live.")
    else:
        print("  ✓  Edge appears stable across time periods.")
    print(line)



def main():
    ap = argparse.ArgumentParser(description="CryptoBot backtester")
    ap.add_argument("--pair",     default="XBTUSD")
    ap.add_argument("--interval", type=int, default=60,
                    help="Candle interval in minutes (Kraken fetch only)")
    ap.add_argument("--csv",      default="",  help="Path to candle CSV")
    ap.add_argument("--walk-forward", action="store_true",
                    help="Run walk-forward analysis across sliding windows")
    ap.add_argument("--wf-window", type=int, default=30,
                    help="Walk-forward window size in days (default 30)")
    ap.add_argument("--wf-step",   type=int, default=15,
                    help="Walk-forward step size in days (default 15)")
    args = ap.parse_args()

    if args.csv:
        if not os.path.exists(args.csv):
            print(f"CSV not found: {args.csv}")
            sys.exit(1)
        candles = load_csv(args.csv)
        interval = args.interval
        name = os.path.splitext(os.path.basename(args.csv))[0]
    else:
        print(f"Fetching Kraken OHLC: {args.pair} interval={args.interval}m …")
        candles = fetch_kraken(args.pair, args.interval)
        interval = args.interval
        name = f"{args.pair}_{args.interval}m"

    if not candles:
        print("No candles loaded. Exiting.")
        sys.exit(1)

    if args.walk_forward:
        walk_forward(candles, args.pair, interval,
                     window_days=args.wf_window, step_days=args.wf_step)
        return   # walk-forward already ran a full-dataset summary internally

    run(candles, args.pair, name, verbose=True)


if __name__ == "__main__":
    main()
