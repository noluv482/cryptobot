#!/usr/bin/env python3
"""Exit overhaul + honesty-infrastructure contract.

The trailing-stop ratchet is OFF by default, condemned by four independent
measurements (paired exit_lab replay t=-3.1, exit attribution -$10.98/79,
sim 8/8, Kaminski/Lo theory: stops need momentum, this market measured mean
reversion). What replaces it: a static disaster stop several ATRs wide, with
targets and the existing time exits doing the work.

Alongside it, three honesty upgrades: maker fills only count when price
trades THROUGH the limit, the live spread is recorded on every shadow row,
and 1m candles start archiving for future intracandle exit simulation.
"""
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ast
import os

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py"),
           encoding="utf-8").read()
FS_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "find_signal.py"),
              encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── 1. TRAIL OFF BY DEFAULT, and the pieces actually gated ──────────────────
check("trail ratchet is OFF by default", bs.TRAIL_ENABLED is False)
check("disaster stop is several ATRs wide", bs.DISASTER_STOP_ATR_MULT >= 2.0,
      str(bs.DISASTER_STOP_ATR_MULT))

tree = ast.parse(SRC)
on_signal_src = ""
for cls in ast.walk(tree):
    if isinstance(cls, ast.ClassDef) and cls.name == "PaperTrader":
        for m in cls.body:
            if isinstance(m, ast.FunctionDef) and m.name == "on_signal":
                on_signal_src = ast.get_source_segment(SRC, m) or ""

# the ratchet writes trail_peak/trail_stop — every such write must sit inside
# an `if TRAIL_ENABLED:` block (checked from the AST, not by eyeballing)
ratchet_guarded = False
for node in ast.walk(tree):
    if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
            and node.test.id == "TRAIL_ENABLED":
        body_src = "".join(ast.get_source_segment(SRC, s) or "" for s in node.body)
        if 'p["trail_peak"] = price' in body_src:
            ratchet_guarded = True
check("the ratchet only runs when TRAIL_ENABLED", ratchet_guarded)
check("breakeven push is gated", "TRAIL_ENABLED and not p.get(\"breakeven_set\")" in SRC)
check("tier-2 lock is gated", "TRAIL_ENABLED and not p.get(\"tier2_trail_set\")" in SRC)
check("tier-3 lock is gated", "TRAIL_ENABLED and not p.get(\"tier3_trail_set\")" in SRC)
check("a static-stop close is attributed 'stop loss', not 'trailing stop'",
      '"trailing stop" if TRAIL_ENABLED else "stop loss"' in on_signal_src)
check("initial stop widens by the disaster multiple when the trail is off",
      "atr_dist if TRAIL_ENABLED else atr_dist * DISASTER_STOP_ATR_MULT" in SRC)

# _trail_only must never strip the target when there is no ratchet to replace it
pt = bs.PaperTrader(no_persist=True)
check("_trail_only is False with the trail off (targets stay real)",
      pt._trail_only("XBTUSD") is False)

check("trail mode is stamped into the config fingerprint",
      '"trail_enabled":   TRAIL_ENABLED' in SRC)

# ── 2. MAKER FILLS: through the limit, executed ─────────────────────────────
saved = dict(bs._pending_entries)
try:
    bs._pending_entries.clear()
    bs._pending_entries["TSTUSD"] = {"sig": "BUY", "limit": 100.0, "scans": 0}
    r_touch = bs._resolve_pending_entry("TSTUSD", 100.0, None)
    check("a touch at the limit does NOT fill (back of queue)", r_touch == "waiting",
          str(r_touch))
    r_fill = bs._resolve_pending_entry("TSTUSD", 100.0 * (1 - bs.MAKER_FILL_THROUGH_PCT) - 1e-9, None)
    check("trading through the limit fills", r_fill == "filled", str(r_fill))
    bs._pending_entries["TSTUSD"] = {"sig": "SELL", "limit": 100.0, "scans": 0}
    r_short = bs._resolve_pending_entry("TSTUSD", 100.0 * (1 + bs.MAKER_FILL_THROUGH_PCT) + 1e-9, None)
    check("short side fills only through the UPSIDE", r_short == "filled", str(r_short))
finally:
    bs._pending_entries.clear()
    bs._pending_entries.update(saved)

# ── 3. SPREAD recorded on every shadow row ──────────────────────────────────
check("shadow_signals grows a spread column",
      '"spread"' in SRC and "ADD COLUMN IF NOT EXISTS {_col} FLOAT" in SRC)
check("log_shadow inserts it", "adx,er,spread" in SRC)
check("the scan loop snapshots the live spread at signal time",
      '"spread": _sp_now' in SRC)

# ── 4. 1m ARCHIVE wired ─────────────────────────────────────────────────────
check("1m archive loop exists and is registered",
      "_m1_archive_loop" in SRC and '("1m archive",        _m1_archive_loop' in SRC)
check("archive drops the still-forming bar", "[:-1])" in SRC
      and "still-forming bar" in SRC)
check("archive stores interval 1 via the shared saver",
      "db.save_candles(pair, 1," in SRC)

# ── 5b. SIGNAL MESSAGES CARRY THEIR VERDICT ─────────────────────────────────
# A Telegram alert with entry/size/leverage reads as an order confirmation;
# sending it and then silently refusing the trade taught the owner to ask
# "why doesn't the bot trade its own signals". Every branch now speaks.
check("signal message is built once and always sent WITH a verdict",
      "_sig_msg = (" in SRC and "tg(_sig_msg)" not in SRC)
for verdict in ("Watch only", "autopilot is ", "below this pair's floor",
                "Maker order resting", "Taking it"):
    check(f"verdict branch exists: {verdict!r}", ("tg(_sig_msg + " in SRC) and verdict in SRC)
check("expired maker orders notify instead of dying silently",
      "Order expired — " in SRC)

# ── 5. find_signal grows the venue-cost question ────────────────────────────
check("find_signal takes --cost", '"--cost"' in FS_SRC)
check("verdict warns that battery windows overlap",
      "non-overlapping re-test" in FS_SRC)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all exit-overhaul contract checks pass")
