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
  - net of costs, two scenarios (both legs, round trip):
      maker: spot 0.16%x2 + perp 0.02%x2 = 0.36%
      taker: spot 0.26%x2 + perp 0.05%x2 = 0.62%
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
COST_MAKER = 0.0036          # both legs, round trip
COST_TAKER = 0.0062
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


def main():
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
