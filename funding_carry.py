#!/usr/bin/env python3
"""Measure funding-rate carry on Kraken Futures — the collect-don't-race test.

Every prediction-based door is measured shut (price signals, order flow,
lead-lag). Carry is structurally different: long spot + short perpetual of
equal notional is (near) delta-neutral, and when funding is positive the
short side is PAID the funding rate on notional every hour. Nobody is being
out-raced — the position is paid to exist. The question is only whether the
payments, over a real year, beat the cost of putting the position on and the
stretches where funding flips negative.

Data: Kraken Futures public v4 historicalfundingrates — hourly
`relativeFundingRate` (fraction of mark price paid per hour), ~1 year back.
No auth, no key, real venue = the venue Noluv would actually use.

Measured per symbol, and honestly:
  - gross carry: sum of hourly rates received by the short leg (negative
    hours SUBTRACT — always-on means eating them)
  - time-positive %, worst 30-day stretch, longest negative run
  - net of costs, two scenarios (all four legs, round trip). The fee numbers
    are IMPORTED from bot_server (KRAKEN_FEE / KRAKEN_MAKER_FEE /
    KRAKEN_FUTURES_FEE / SLIPPAGE), not hardcoded here, and the exact values
    used are printed in the output header.
  - a NAIVE FILTER (hold only while trailing 7-day mean funding > 0),
    charged full round-trip costs on every flip — judged on the SECOND
    half of the year only, with the filter's one parameter (7d) fixed in
    advance, not fitted. First half is shown for context.
  - capital honesty: carry accrues on NOTIONAL. Real capital = spot notional
    + perp margin (assume 50% buffer) = 1.5x notional, so APR on capital is
    the notional APR / 1.5. Both are printed.

NOT modeled (all hurt, none fatal, disclose): basis drift between spot and
perp entry/exit, borrow/withdrawal frictions, the spot leg's spread, exchange
risk, and US eligibility for Kraken Futures must be checked before acting.
"""
import json
import statistics
import sys
import urllib.request

SYMS = ["PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "PF_XRPUSD",
        "PF_DOGEUSD", "PF_LINKUSD"]
# ── Costs: ONE source of truth, the bot's own fee constants ─────────────────
# These used to be hardcoded (0.36% maker / 0.62% taker round trip) from
# Kraken's advertised low-tier schedule, while the live container models
# KRAKEN_FEE=0.8%/side taker and KRAKEN_MAKER_FEE=0.4%/side maker plus
# SLIPPAGE. The two disagreed by ~2x, which meant this script's "net" and the
# bot's "net" were not the same number and nobody could tell which one a
# decision came from. It now IMPORTS the bot's constants, so a fee change in
# one place moves both. The fallback (running this file with no bot_server on
# the path) uses bot_server's own documented DEFAULTS, never the old cheaper
# numbers, and the header below always prints which set was used.
FEE_SOURCE = "bot_server (live constants)"
try:
    import bot_server as _bs
    KRAKEN_FEE       = float(_bs.KRAKEN_FEE)            # spot taker, per side
    KRAKEN_MAKER_FEE = float(_bs.KRAKEN_MAKER_FEE)      # spot maker, per side
    FUTURES_FEE      = float(_bs.KRAKEN_FUTURES_FEE)    # perp taker, per side
    SLIPPAGE         = float(_bs.SLIPPAGE)
except Exception as _e:                                  # standalone fallback
    FEE_SOURCE = f"standalone fallback defaults ({type(_e).__name__})"
    KRAKEN_FEE, KRAKEN_MAKER_FEE, FUTURES_FEE, SLIPPAGE = 0.008, 0.004, 0.0005, 0.001

# Four legs: open spot + open perp, close spot + close perp. The perp leg is
# charged the TAKER futures fee in both scenarios (no maker assumption is
# defensible for the hedge leg), so "maker" only changes the spot side.
COST_MAKER = 2 * (KRAKEN_MAKER_FEE + SLIPPAGE) + 2 * (FUTURES_FEE + SLIPPAGE)
COST_TAKER = 2 * (KRAKEN_FEE + SLIPPAGE) + 2 * (FUTURES_FEE + SLIPPAGE)
MARGIN_MULT = 1.5            # capital = 1.5x notional (spot + margin buffer)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-research"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch(sym):
    d = get("https://futures.kraken.com/derivatives/api/v4/"
            f"historicalfundingrates?symbol={sym}")
    rates = [(r["timestamp"], float(r["relativeFundingRate"]))
             for r in d.get("rates", [])]
    rates.sort()
    return rates


def stretch_stats(vals):
    """worst rolling 30d sum and longest consecutive <=0 run, hours."""
    worst30 = 0.0
    win = 24 * 30
    s = sum(vals[:win])
    worst30 = s
    for i in range(win, len(vals)):
        s += vals[i] - vals[i - win]
        worst30 = min(worst30, s)
    longest = cur = 0
    for v in vals:
        cur = cur + 1 if v <= 0 else 0
        longest = max(longest, cur)
    return worst30, longest


def cost_header():
    """The exact fee numbers this run used, and where they came from. Printed
    first so no output of this script is ever ambiguous about its cost model."""
    return (f"costs: source={FEE_SOURCE} | spot taker {KRAKEN_FEE*100:.3f}%/side, "
            f"spot maker {KRAKEN_MAKER_FEE*100:.3f}%/side, perp taker "
            f"{FUTURES_FEE*100:.3f}%/side, slippage {SLIPPAGE*100:.3f}%/side "
            f"-> 4-leg round trip: maker {COST_MAKER*100:.2f}%, "
            f"taker {COST_TAKER*100:.2f}%")


def main():
    print(cost_header())
    print(f"{'symbol':12s} {'hours':>6s} {'gross APR':>9s} {'pos%':>5s} "
          f"{'worst 30d':>9s} {'neg-run':>8s} {'net mkr':>8s} {'net tkr':>8s} "
          f"{'on capital':>10s}")
    agg = []
    halves = {}
    for sym in SYMS:
        try:
            rates = fetch(sym)
        except Exception as e:
            print(f"{sym:12s} FETCH FAILED: {e}")
            continue
        if len(rates) < 24 * 60:
            print(f"{sym:12s} only {len(rates)} hours — skipped")
            continue
        vals = [v for _, v in rates]
        years = len(vals) / (24 * 365)
        gross = sum(vals)                      # received by the short leg
        apr = gross / years
        pos = 100 * sum(1 for v in vals if v > 0) / len(vals)
        worst30, negrun = stretch_stats(vals)
        net_m = apr - COST_MAKER / years       # one entry+exit over the period
        net_t = apr - COST_TAKER / years
        print(f"{sym:12s} {len(vals):>6,} {apr*100:>8.2f}% {pos:>4.0f}% "
              f"{worst30*100:>8.2f}% {negrun:>6}h {net_m*100:>7.2f}% "
              f"{net_t*100:>7.2f}% {net_m/MARGIN_MULT*100:>9.2f}%")
        agg.append(net_m / MARGIN_MULT)
        # naive pre-fixed filter, judged OOS on the second half
        mid = len(vals) // 2
        for name, seg in (("IS ", vals[:mid]), ("OOS", vals[mid:])):
            held = flips = 0
            earned = 0.0
            holding = False
            w = 24 * 7
            for i in range(w, len(seg)):
                trail = sum(seg[i - w:i]) / w
                want = trail > 0
                if want != holding:
                    flips += 1
                    holding = want
                if holding:
                    earned += seg[i]
                    held += 1
            seg_years = (len(seg) - w) / (24 * 365)
            net = (earned - flips / 2 * COST_MAKER) / seg_years
            halves.setdefault(sym, {})[name] = (net, flips, held / (len(seg) - w))
    print("\n7d-trailing filter (>0 hold), maker costs on every flip — "
          "OOS = second half only:")
    for sym, h in halves.items():
        i, o = h.get("IS "), h.get("OOS")
        if i and o:
            print(f"  {sym:12s} IS {i[0]*100:+6.2f}%/yr ({i[1]} flips, "
                  f"held {i[2]*100:.0f}%)   OOS {o[0]*100:+6.2f}%/yr "
                  f"({o[1]} flips, held {o[2]*100:.0f}%)")
    if agg:
        print(f"\nequal-weight basket, net maker, on CAPITAL (1.5x notional): "
              f"{statistics.fmean(agg)*100:+.2f}%/yr across {len(agg)} symbols")
        print("caveats NOT in these numbers: basis drift at entry/exit, spot "
              "spread, negative-funding regimes can persist, exchange risk, "
              "and US eligibility for Kraken Futures must be verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
