#!/usr/bin/env python3
"""Persistence + backfill for the bot's trade notes, run against the real
source in bot_server.py.

Covers the three ways a store like this actually breaks: it silently loses the
oldest entries, it rewrites something the user already has, or it re-does its
work on every boot and grinds.
"""
import ast
import json
import os
import sys
import tempfile
import threading
import time

SRC = r"C:\Users\jhanp\New folder\CryptoTrader\bot_server.py"

FUNCS = {"_note_price", "_auto_note_key", "_auto_note_ms", "_note_history",
         "_auto_note_tags", "_build_auto_note", "_build_auto_note_at",
         "_auto_notes_load", "_auto_notes_save", "_auto_note_record",
         "_auto_notes_backfill", "_decode_fkey"}
CLASSES = {"_TradesUpTo"}
ASSIGNS = {"_PILLAR_WORDS", "AUTO_NOTES_KEEP"}

LOGS = []


def load(tmpdir):
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"time": time, "json": json, "os": os, "threading": threading,
          "log": lambda *a, **k: LOGS.append(" ".join(str(x) for x in a)),
          "AUTO_NOTES_FILE": os.path.join(tmpdir, "auto_notes.json"),
          "_auto_notes_lock": threading.Lock()}
    picked = set()
    for node in tree.body:
        take = False
        if isinstance(node, ast.FunctionDef) and node.name in FUNCS:
            take = True; picked.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name in CLASSES:
            take = True; picked.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ASSIGNS:
                    take = True; picked.add(t.id)
        if take:
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
    missing = (FUNCS | CLASSES | ASSIGNS) - picked
    if missing:
        print(f"FAIL: missing from bot_server.py: {sorted(missing)}")
        sys.exit(1)
    return ns


class FakeTrader:
    def __init__(self, trades): self.trades = trades
    @property
    def consecutive_losses(self):
        n = 0
        for t in reversed(self.trades):
            if t.get("pnl", 0) <= 0: n += 1
            else: break
        return n


def trade(i, pnl=1.0):
    return {"side": "LONG", "entry": 100.0, "exit": 101.0, "pnl": pnl,
            "coin": "SOL", "pair": "SOLUSD", "confidence": 0.5,
            "held_mins": 20.0, "reason": "take profit",
            "ts": 1700000000.0 + i * 3600, "fkey": "e1m1", "hour": 12,
            "pillars": {"rsi_zone": True, "macd_align": True}}


def main():
    tmp = tempfile.mkdtemp(prefix="notetest_")
    ns = load(tmp)
    path = ns["AUTO_NOTES_FILE"]
    fails = []
    P = print

    P("=" * 72); P("  STORE"); P("=" * 72)

    # Missing file must read as empty, not crash.
    if ns["_auto_notes_load"]() != {}:
        fails.append("missing file did not load as {}")
    else:
        P("  missing file  -> {} (ok)")

    # Corrupt file must read as empty, not crash — a half-written JSON must not
    # take the dashboard down.
    with open(path, "w") as f:
        f.write("{not json")
    if ns["_auto_notes_load"]() != {}:
        fails.append("corrupt file did not load as {}")
    else:
        P("  corrupt file  -> {} (ok)")
    os.remove(path)

    # Round trip.
    t = trade(1)
    ns["_auto_note_record"](FakeTrader([t]), {}, t)
    got = ns["_auto_notes_load"]()
    key = ns["_auto_note_key"](t["ts"], t["pair"])
    if key not in got:
        fails.append(f"record() did not write key {key}")
    else:
        P(f"  round trip    -> {key} (ok)")
    if not os.path.exists(path):
        fails.append("no file on disk after record()")

    P(); P("=" * 72); P("  CAP — oldest dropped, newest kept"); P("=" * 72)
    cap = ns["AUTO_NOTES_KEEP"]
    trades = [trade(i) for i in range(cap + 25)]
    for i, tr in enumerate(trades):
        ns["_auto_note_record"](FakeTrader(trades[:i + 1]), {}, tr)
    got = ns["_auto_notes_load"]()
    P(f"  wrote {len(trades)}, cap is {cap}, stored {len(got)}")
    if len(got) > cap:
        fails.append(f"cap not enforced: {len(got)} > {cap}")
    newest = ns["_auto_note_key"](trades[-1]["ts"], trades[-1]["pair"])
    oldest = ns["_auto_note_key"](trades[0]["ts"], trades[0]["pair"])
    if newest not in got:
        fails.append("cap dropped the NEWEST note")
    else:
        P("  newest kept (ok)")
    if oldest in got:
        fails.append("cap kept the oldest note instead of dropping it")
    else:
        P("  oldest dropped (ok)")

    P(); P("=" * 72); P("  BACKFILL"); P("=" * 72)
    os.remove(path)
    hist = [trade(i, pnl=1.0 if i % 3 else -1.0) for i in range(40)]
    tr = FakeTrader(hist)
    ns["_auto_notes_backfill"](tr)
    first = ns["_auto_notes_load"]()
    P(f"  first boot  -> {len(first)} notes for {len(hist)} trades")
    if len(first) != len(hist):
        fails.append(f"backfill wrote {len(first)} notes for {len(hist)} trades")

    # A note the user's bot already wrote must survive a second boot untouched.
    k = ns["_auto_note_key"](hist[5]["ts"], hist[5]["pair"])
    first[k]["text"] = "SENTINEL — must not be overwritten"
    ns["_auto_notes_save"](first)
    ns["_auto_notes_backfill"](tr)
    second = ns["_auto_notes_load"]()
    if second[k]["text"] != "SENTINEL — must not be overwritten":
        fails.append("backfill overwrote an existing note")
    else:
        P("  second boot leaves existing notes alone (ok)")
    if len(second) != len(first):
        fails.append(f"second backfill changed count {len(first)} -> {len(second)}")
    else:
        P("  second boot is a no-op (ok)")

    # A trade with no timestamp must be skipped, not keyed as '0|'.
    os.remove(path)
    broken = hist + [dict(trade(99), ts=0)]
    ns["_auto_notes_backfill"](FakeTrader(broken))
    got = ns["_auto_notes_load"]()
    if any(k.startswith("0|") for k in got):
        fails.append("a trade with ts=0 was given a bogus key")
    else:
        P("  ts=0 trade skipped (ok)")

    # Backfill must never raise, whatever the record looks like.
    os.remove(path)
    junk = [{"pnl": 1.0, "ts": 1700000000.0, "pair": "X"},   # nearly empty
            {"ts": 1700000001.0, "pair": "Y", "pnl": None},   # None pnl
            {}]                                               # nothing at all
    LOGS.clear()
    try:
        ns["_auto_notes_backfill"](FakeTrader(junk))
        P("  malformed records did not raise (ok)")
    except Exception as e:
        fails.append(f"backfill raised on malformed records: {type(e).__name__}: {e}")

    P(); P("=" * 72)
    if fails:
        P(f"  {len(fails)} FAILURE(S)")
        for f in fails: P("   x " + f)
        return 1
    P("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
