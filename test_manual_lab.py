#!/usr/bin/env python3
"""manual_lab capture contract.

The manual book records entry/exit/pnl only, which cannot teach anything —
there is no feature to learn FROM. manual_lab adds the bot's view at the moment
the human acted, which is what makes a hand trade learnable.

Three things must hold, and the third is the one that matters most:

  1. WIRED — both manual close paths (user close, and SL/TP/liquidation from
     the scan loop) stamp the outcome. Verified by parsing the SOURCE, not by
     trusting a hand-written fixture: the 45h stall happened because a test
     asserted the shape the consumer expected instead of the shape the
     producer writes.
  2. SAFE — every lab call degrades silently with no DB. A learning hook must
     never break the owner's ability to place or close a trade.
  3. ISOLATED — human trades must never enter the bot's own statistics. Those
     stats are the thing being measured; polluting them with hand trades would
     corrupt the measurement the whole project rests on.
"""
import ast
import os
import sys

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py")
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def calls_in(func_name):
    """Every attribute call like db.foo(...) inside a top-level function."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    out.add(sub.func.attr)
            return out
    return None


# 1. WIRED — derived from the source, not assumed
opened = calls_in("_manual_open")
check("_manual_open exists", opened is not None)
check("_manual_open records to the lab", bool(opened and "log_manual" in opened),
      str(sorted(opened or [])[:8]))
check("_manual_open captures the bot's view",
      bool(opened and "bot_view_of" in opened))

for path in ("_manual_close", "_manual_check_exits"):
    c = calls_in(path)
    check(f"{path} exists", c is not None)
    check(f"{path} stamps the outcome (close_manual)", bool(c and "close_manual" in c),
          str(sorted(c or [])[:8]))

# 2. SAFE — no DB must never raise
saved = bs.db.conn
try:
    bs.db.conn = None
    check("log_manual survives no DB", bs.db.log_manual({"pair": "X"}) is None)
    check("bot_view_of survives no DB", bs.db.bot_view_of("X", 0) is None)
    check("manual_pending survives no DB", bs.db.manual_pending(0) == [])
    ok = True
    try:
        bs.db.close_manual("X", 0, 0, 0, 0, "r")
        bs.db.fill_manual(1, 0, 0, 0)
    except Exception:
        ok = False
    check("close_manual/fill_manual survive no DB", ok)
finally:
    bs.db.conn = saved

# 3. ISOLATED — the human's book must not touch the bot's trader
tree = ast.parse(open(SRC, encoding="utf-8").read())
leaks = []
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name.startswith("_manual_"):
        for sub in ast.walk(node):
            # a manual path may READ trader state, but must never record a
            # trade into it (on_signal/close would enter the bot's own stats)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr in ("on_signal", "_open", "log_trade"):
                    leaks.append(f"{node.name} -> {sub.func.attr}")
check("manual paths never write into the bot's own trade stats", not leaks, str(leaks))

# the lab table must be its own table, not shadow_signals
src = open(SRC, encoding="utf-8").read()
check("manual_lab is a separate table", "CREATE TABLE IF NOT EXISTS manual_lab" in src)
check("manual rows carry the gate the human overrode", "bot_gate" in src)
check("'never evaluated' is distinct from 'taken'", "NOT_EVALUATED" in src)

# 4. WRITE-ONLY — the bot must never read its owner's trades back
# If the bot adapted to his trades while he watches the bot's signals, the two
# stop being independent samples and BOTH measurement systems are destroyed at
# once — and irreversibly, because past data cannot be de-contaminated. So the
# lab is write-only from the trading side: only the logging helpers, the
# backfill loop, and the offline report may touch it.
ALLOWED = {
    "_manual_open", "_manual_close", "_manual_check_exits", "_manual_reset",
    "_learning_filler_loop",
    # the DB helpers themselves
    "log_manual", "close_manual", "fill_manual", "manual_pending",
    "censor_open_manual", "bot_view_of",
    "_init", "_init_schema",   # the CREATE/ALTER lives here by definition
}
LAB_CALLS = {"log_manual", "close_manual", "fill_manual", "manual_pending",
             "censor_open_manual"}
readers = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in ALLOWED:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr in LAB_CALLS:
                readers.append(f"{node.name} -> {sub.func.attr}")
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and "manual_lab" in sub.value \
                    and any(k in sub.value.upper()
                            for k in ("FROM", "INTO", "UPDATE", "TABLE", "JOIN")):
                # SQL against the table, not prose that merely names it — a
                # docstring saying "never touches manual_lab" is the guard
                # being documented, not broken.
                readers.append(f"{node.name} -> SQL(manual_lab)")
check("no trading path reads the manual lab (feedback-loop guard)",
      not readers, str(sorted(set(readers))[:5]))

# 5. CENSORING — the guards that make a growing sample trustworthy
check("the PLAN is recorded at open (stop/target/horizon)",
      all(k in src for k in ("plan_stop", "plan_target", "plan_horizon_h")))
check("book reset closes open rows instead of dropping them",
      "censor_open_manual" in src and "BOOK_RESET" in src)
check("a close that matches no open row is logged, not swallowed",
      "censored from the stats" in src)
check("real round-trip cost is snapshotted per row", "fee_rt_pct" in src)
check("contamination flag is derived, not self-reported",
      all(k in src for k in ("ECHO", "CONTRA", "INDEPENDENT")))
check("exits are classified as planned vs discretionary",
      all(k in src for k in ("PLAN_TARGET", "PLAN_STOP", "DISCRETIONARY")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all manual-lab contract checks pass")
