#!/usr/bin/env python3
"""Sim persistence contract — a restart must not reset the sim book.

Tests against the REAL class (import bot_server), not a hand-built fixture,
because the 45h-stall bug taught us fixtures drift from producers.

  1. round trip: balance / trades / positions written by _save() come back
     identically in a fresh instance (simulating a container restart)
  2. the sim_enabled flag rides along in the same file
  3. isolation: loading the sim's file must NOT flip the global _paper_mode
     (._apply_state writes that global; the sim load path must shield it)
  4. a backtest instance (no state_path) writes nothing to disk
  5. atomicity artifact: no leftover .tmp after a save
"""
import json
import os
import sys
import tempfile

import bot_server as bs

bs.log = lambda *a, **k: None
FAILS = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "sim_state.json")

    # 1+2: write a book, "restart", read it back
    a = bs.PaperTrader(force_paper=True, start_balance=2000.0, state_path=path)
    a.balance = 2121.44
    a.trades = [{"pnl": 12.4, "pair": "SOLUSD"}, {"pnl": 9.04, "pair": "ETHUSD"}]
    a.positions = {"HYPEUSD": {"side": "LONG", "entry": 75.48, "margin": 400.0,
                               "name": "HYPE/USD", "opened_at": 1787000000.0}}
    bs._sim_enabled = True
    a._save()
    check("save writes the file", os.path.exists(path))
    check("no leftover .tmp", not os.path.exists(path + ".tmp"))

    pm_before = bs._paper_mode
    b = bs.PaperTrader(force_paper=True, start_balance=2000.0, state_path=path)
    check("balance survives restart", abs(b.balance - 2121.44) < 1e-9)
    check("trades survive restart", len(b.trades) == 2 and b.trades[0]["pnl"] == 12.4)
    check("positions survive restart",
          "HYPEUSD" in b.positions and b.positions["HYPEUSD"]["entry"] == 75.48)
    check("sim file does not flip global _paper_mode", bs._paper_mode == pm_before)
    with open(path) as f:
        check("sim_enabled flag persisted", json.load(f).get("sim_enabled") is True)

    # 4: backtest instances stay memory-only
    before = set(os.listdir(td))
    c = bs.PaperTrader(no_persist=True)
    c.balance = 123.0
    c._save()
    check("backtest instance writes nothing", set(os.listdir(td)) == before)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all sim-persistence checks pass")
