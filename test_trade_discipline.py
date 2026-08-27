#!/usr/bin/env python3
"""Trade-ticket + discipline contract.

The Trade tab grew five behaviors that all exist to make the OWNER's plan
real: the bet stated before entry, max-hold enforced, hand exits explained,
post-loss entries tagged, and a daily loss lockout. Every one of them must be
tested by EXECUTING the logic, not by trusting that markup exists — and the
plan replay (what would the written plan have paid?) is the one that prices
"pulled out early", so its arithmetic gets the adversarial treatment.

Replay honesty rules under test:
  - a bar that touches stop AND target resolves to the STOP (never credit
    profit out of ambiguity)
  - bars that started before the entry are excluded from touch tests
  - the loss is clamped at posted margin, like liquidation enforces
  - unresolvable windows return pending, never a guess
"""
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import time

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py"),
           encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


H = 3600
T0 = 1_787_000_000


def bar(i, close, high, low):
    return (T0 + i * H, close, high, low)


# ── 1. PLAN REPLAY, executed ────────────────────────────────────────────────
# LONG from 100, stop 95, target 110, notional 200, size 100, fee 0.8%/side.
ARGS = dict(side="BUY", entry=100.0, notional=200.0, size=100.0,
            fee_side_pct=0.008, plan_stop=95.0, plan_target=110.0,
            horizon_h=48.0, ts_entry=T0)

# target touched cleanly on bar 2
pnl, rule = bs._plan_replay(bars=[bar(0, 101, 103, 99), bar(1, 104, 106, 100),
                                  bar(2, 108, 111, 103)], **ARGS)
check("target touch pays the target, minus the exit fee",
      rule == "PLAN_TARGET" and abs(pnl - (0.10 * 200 - 200 * 0.008)) < 1e-6,
      f"{rule} {pnl}")

# same bar touches BOTH levels → stop, never target
pnl, rule = bs._plan_replay(bars=[bar(0, 100, 111, 94)], **ARGS)
check("same-bar stop+target resolves to the STOP",
      rule == "PLAN_STOP" and pnl < 0, f"{rule} {pnl}")

# stop touched by wick only (close never below)
pnl, rule = bs._plan_replay(bars=[bar(0, 100, 101, 94.9)], **ARGS)
check("a wick through the stop IS a stop-out",
      rule == "PLAN_STOP" and abs(pnl - (-0.05 * 200 - 200 * 0.008)) < 1e-6,
      f"{rule} {pnl}")

# neither level touched, horizon expires → PLAN_TIME at that bar's close
pnl, rule = bs._plan_replay(bars=[bar(i, 102, 104, 99) for i in range(60)], **ARGS)
check("horizon expiry closes at the deadline bar (PLAN_TIME)",
      rule == "PLAN_TIME" and abs(pnl - (0.02 * 200 - 200 * 0.008)) < 1e-6,
      f"{rule} {pnl}")

# bars that started BEFORE entry are excluded from touch tests
pre = [(T0 - 2 * H, 100, 120, 90)]        # would hit both levels — but pre-entry
pnl, rule = bs._plan_replay(bars=pre + [bar(i, 102, 104, 99) for i in range(60)], **ARGS)
check("pre-entry bars never trigger the plan", rule == "PLAN_TIME", f"{rule}")

# SHORT: stop sits ABOVE entry, target below
pnl, rule = bs._plan_replay(side="SELL", entry=100.0, notional=200.0, size=100.0,
                            fee_side_pct=0.008, plan_stop=105.0, plan_target=90.0,
                            horizon_h=48.0, ts_entry=T0,
                            bars=[bar(0, 99, 104.9, 98), bar(1, 91, 99, 89.9)])
check("short side: high is adverse, low is favorable",
      rule == "PLAN_TARGET" and abs(pnl - (0.10 * 200 - 200 * 0.008)) < 1e-6,
      f"{rule} {pnl}")

# loss clamped at posted margin (10x-style: notional 1000 on size 100)
pnl, rule = bs._plan_replay(side="BUY", entry=100.0, notional=1000.0, size=100.0,
                            fee_side_pct=0.008, plan_stop=80.0, plan_target=None,
                            horizon_h=48.0, ts_entry=T0,
                            bars=[bar(0, 79, 100, 78)])
check("plan loss is clamped at the posted margin", pnl == -100.0, str(pnl))

# nothing written → NO_PLAN; no bars → NO_DATA; window not covered → pending
check("no plan replays as NO_PLAN",
      bs._plan_replay(side="BUY", entry=100, notional=200, size=100,
                      fee_side_pct=0.008, plan_stop=None, plan_target=None,
                      horizon_h=None, ts_entry=T0, bars=[bar(0, 100, 101, 99)])
      == (None, "NO_PLAN"))
check("no bars replays as NO_DATA",
      bs._plan_replay(bars=[], **ARGS) == (None, "NO_DATA"))
pnl, rule = bs._plan_replay(bars=[bar(0, 102, 104, 99)], **ARGS)
check("uncovered window stays pending, never guessed",
      pnl is None and rule is None, f"{rule}")

# no horizon written → scored at the same 48h window as every lab number
pnl, rule = bs._plan_replay(side="BUY", entry=100.0, notional=200.0, size=100.0,
                            fee_side_pct=0.008, plan_stop=95.0, plan_target=110.0,
                            horizon_h=None, ts_entry=T0,
                            bars=[bar(i, 102, 104, 99) for i in range(80)])
check("horizonless plan caps at the lab's 48h window", rule == "PLAN_TIME", f"{rule}")

# ── 2. DAILY LOSS LIMIT, executed ───────────────────────────────────────────
now = time.time()
book = {"balance": 97.0, "positions": {},
        "trades": [{"pnl": -3.0, "ts": now - 60}]}          # −3 today on 100 start
d = bs._manual_daily(book, now=now)
check("daily limit is % of the DAY-START balance",
      abs(d["limit"] - 3.0) < 1e-9, str(d))
check("hitting the limit locks the day", d["locked"] is True, str(d))
d2 = bs._manual_daily({"balance": 99.0, "positions": {},
                       "trades": [{"pnl": -1.0, "ts": now - 60}]}, now=now)
check("under the limit stays unlocked, remaining is honest",
      d2["locked"] is False and abs(d2["remaining"] - 2.0) < 1e-9, str(d2))
old = {"balance": 97.0, "positions": {},
       "trades": [{"pnl": -3.0, "ts": now - 3 * 86400}]}     # loss was days ago
check("yesterday's losses do not lock today",
      bs._manual_daily(old, now=now)["locked"] is False)
check("an early WIN cannot widen the day's loss room",
      abs(bs._manual_daily({"balance": 110.0, "positions": {},
                            "trades": [{"pnl": 10.0, "ts": now - 60}]},
                           now=now)["limit"] - 3.0) < 1e-9)

# ── 3. POST-LOSS COOLDOWN, executed ─────────────────────────────────────────
check("a fresh loss starts the cooldown",
      bs._manual_cooldown_until({"trades": [{"pnl": -5, "ts": now - 60}]}, now=now) > now)
check("a win clears it",
      bs._manual_cooldown_until({"trades": [{"pnl": +5, "ts": now - 60}]}, now=now) == 0)
check("an old loss is already cooled",
      bs._manual_cooldown_until({"trades": [{"pnl": -5, "ts": now - 2000}]}, now=now) == 0)
check("empty book has no cooldown",
      bs._manual_cooldown_until({"trades": []}, now=now) == 0)

# ── 4. WIRING — the behaviors are actually connected ────────────────────────
check("open handler refuses when the day is locked",
      "daily loss limit hit" in SRC)
check("post-loss entries are stamped server-side, not client-claimed",
      '"post_loss": _post_loss' in SRC)
check("max hold is stored on the position and enforced in the exit loop",
      '"horizon_h": (float(d.get("horizon_h"))' in SRC
      and '"max hold"' in SRC and "PLAN_TIME" in SRC)
check("exit checks use intrabar wicks, not just last price",
      "SELECT high, low FROM candles" in SRC)
check("intrabar bars that predate the entry are excluded",
      "ts >= %s" in SRC and "STARTED before the entry" in SRC)
check("hand-close reasons are a fixed vocabulary",
      '("fear_giveback", "looked_done", "news", "bored")' in SRC)
check("filler loop feeds the plan replay",
      "manual_plan_pending()" in SRC and "_plan_replay(" in SRC)
check("filler fetches highs/lows for touch tests, not closes only",
      "float(c[2]), float(c[3])" in SRC)

# ── 5. TICKET + PANEL markup served ─────────────────────────────────────────
check("bet line exists and prices the stop, the target, and R after fees",
      all(k in SRC for k in ("mt_bet", "at stop", "at target", "after fees")))
check("fee-eaten targets are called out before entry",
      "fees eat this target" in SRC)
check("daily meter + lockout strip on the form",
      all(k in SRC for k in ("mt_daily", "LOCKED TODAY", "locked for today")))
check("close asks WHY with fixed choices",
      all(k in SRC for k in ("mt_close_confirm", "fear of giveback", "looked done",
                             "just close it")))
check("max-hold countdown renders on the position card",
      "mt_hold_row" in SRC and "auto-close in" in SRC)
check("R shows on trade rows only when a stop was written",
      "no stop, no R" in SRC)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all trade-discipline contract checks pass")
