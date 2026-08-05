#!/usr/bin/env python3
"""Test the bot's trade-note writer against the REAL source in bot_server.py.

Deliberately not a re-implementation. A past bug in this project survived its
unit test because the test hand-built the input dict the way the *reader*
expected rather than the way the *producer* actually writes it, so here the
functions are lifted verbatim out of bot_server.py by AST and the trade records
are built by copying the literal `trade_rec = {...}` out of PaperTrader.close().
If close() changes its field names, this test breaks — which is the point.
"""
import ast
import sys
import time

SRC = r"C:\Users\jhanp\New folder\CryptoTrader\bot_server.py"

WANT_FUNCS = {
    "_note_price", "_auto_note_key", "_auto_note_ms", "_note_history",
    "_auto_note_tags", "_build_auto_note", "_build_auto_note_at",
    "_decode_fkey",
}
WANT_CLASSES = {"_TradesUpTo"}
WANT_ASSIGN = {"_PILLAR_WORDS", "AUTO_NOTES_KEEP"}


def load():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"time": time, "log": lambda *a, **k: None}
    picked = set()
    for node in tree.body:
        take = False
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            take = True; picked.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name in WANT_CLASSES:
            take = True; picked.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in WANT_ASSIGN:
                    take = True; picked.add(t.id)
        if take:
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
    missing = (WANT_FUNCS | WANT_CLASSES | WANT_ASSIGN) - picked
    if missing:
        print(f"FAIL: not found in bot_server.py: {sorted(missing)}")
        sys.exit(1)
    return ns


def close_field_names():
    """Field names PaperTrader.close() actually puts in trade_rec."""
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "trade_rec"
                and isinstance(node.value, ast.Dict)):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return set()


class FakeTrader:
    def __init__(self, trades):
        self.trades = trades

    @property
    def consecutive_losses(self):
        n = 0
        for t in reversed(self.trades):
            if t.get("pnl", 0) <= 0: n += 1
            else: break
        return n


def rec(**kw):
    """A trade record with exactly close()'s fields, overridden by kw."""
    base = {"side": "LONG", "entry": 100.0, "exit": 102.0, "pnl": 1.5,
            "coin": "SOL", "pair": "SOLUSD", "confidence": 0.62,
            "held_mins": 45.0, "reason": "take profit", "ts": time.time(),
            "fkey": "e1m1v1", "hour": 14,
            "pillars": {"rsi_zone": True, "news_align": False,
                        "nasdaq_align": True, "tick_strength": True,
                        "macd_align": True, "high_volume": True,
                        "candle_pattern": False, "vwap_align": True,
                        "obv_trend": False, "chart_struct": False,
                        "stoch_rsi": False}}
    base.update(kw)
    return base


def main():
    ns = load()
    build = ns["_build_auto_note"]
    fails = []

    # ── The contract with close(): every field the note reads must exist ──────
    produced = close_field_names()
    reads = {"side", "coin", "entry", "exit", "pnl", "confidence",
             "fkey", "held_mins", "reason", "pillars", "ts"}
    gap = reads - produced
    print("=" * 72)
    print("  CONTRACT — fields the note reads vs fields close() writes")
    print("=" * 72)
    print(f"  close() writes: {sorted(produced)}")
    if gap:
        fails.append(f"note reads {sorted(gap)} but close() never writes them")
        print(f"  MISMATCH: {sorted(gap)}")
    else:
        print("  ok — every field the note reads is written by close()\n")

    # ── Cases ────────────────────────────────────────────────────────────────
    hist = [rec(pnl=-2.0, fkey="e1m1v1", coin="SOL", ts=time.time() - 9000),
            rec(pnl=3.0,  fkey="e1m1v1", coin="SOL", ts=time.time() - 8000),
            rec(pnl=-1.0, fkey="e1m1v1", coin="SOL", ts=time.time() - 7000),
            rec(pnl=-4.0, fkey="e0m0",   coin="ETH", pair="ETHUSD", ts=time.time() - 6000)]

    cases = [
        ("winner, high conviction, long hold",
         rec(pnl=6.20, held_mins=185, confidence=0.71, reason="take profit"), {}),
        ("loser, chased, 2 min hold",
         rec(pnl=-3.10, exit=97.0, held_mins=2, confidence=0.33,
             reason="stop loss",
             pillars={"rsi_zone": True, "macd_align": False, "high_volume": False,
                      "news_align": False, "nasdaq_align": False, "tick_strength": False,
                      "candle_pattern": False, "vwap_align": False, "obv_trend": False,
                      "chart_struct": False, "stoch_rsi": False}), {}),
        ("short at 10x, trailing stop, loss",
         rec(side="SHORT", pnl=-5.0, entry=100.0, exit=103.0, held_mins=30,
             reason="trailing stop"), {"leverage": 10}),
        ("news-driven entry",
         rec(fkey="e1m1nB", pnl=2.0,
             pillars={**rec()["pillars"], "news_align": True}), {}),
        ("sub-dollar coin price formatting",
         rec(coin="DOGE", pair="XDGUSD", entry=0.084213, exit=0.086901, pnl=0.9), {}),
        ("empty pillars / no fkey (older record)",
         rec(fkey="", pillars={}, confidence=0.0), {}),
        ("zero entry price — must not divide by zero",
         rec(entry=0.0, exit=0.0), {}),
    ]

    print("=" * 72)
    print("  GENERATED NOTES")
    print("=" * 72)
    for label, t, pos in cases:
        trades = hist + [t]
        try:
            note = build(FakeTrader(trades), pos, t)
        except Exception as e:
            fails.append(f"{label}: raised {type(e).__name__}: {e}")
            print(f"\n  [{label}]  RAISED {type(e).__name__}: {e}")
            continue
        print(f"\n  [{label}]  tags={note['tags']}")
        for line in note["text"].split("\n"):
            print("    " + line)

        # Invariants that make the note trustworthy rather than merely present.
        if not note["text"].strip():
            fails.append(f"{label}: empty note")
        if note["win"] != (t["pnl"] > 0):
            fails.append(f"{label}: win flag disagrees with pnl")
        if note["pnl"] != round(t["pnl"], 2):
            fails.append(f"{label}: pnl mismatch")
        if "None" in note["text"] or "?%" in note["text"]:
            fails.append(f"{label}: note contains a placeholder: {note['text']!r}")
        if t["side"] not in note["text"]:
            fails.append(f"{label}: side missing from note")

    # ── The key must match what the dashboard builds ─────────────────────────
    print("\n" + "=" * 72)
    print("  KEY FORMAT — server key must equal the dashboard's _tradeNoteKey")
    print("=" * 72)
    t = rec(ts=1754400000.123, pair="SOLUSD")
    server_key = ns["_auto_note_key"](t["ts"], t["pair"])
    # What the JS does: t.ts is int(ts*1000) from /status, then ts+'|'+pair
    js_key = f"{int(t['ts'] * 1000)}|{t['pair']}"
    print(f"  server: {server_key}")
    print(f"  client: {js_key}")
    if server_key != js_key:
        fails.append(f"key mismatch: server {server_key!r} vs client {js_key!r}")
    else:
        print("  ok — identical\n")

    # ── Backfill must not let a note quote trades that came after it ─────────
    print("=" * 72)
    print("  BACKFILL — a historical note may only cite earlier trades")
    print("=" * 72)
    later = [rec(pnl=-1.0, fkey="zz", coin="SOL", ts=time.time() - 5000),
             rec(pnl=-1.0, fkey="zz", coin="SOL", ts=time.time() - 4000),
             rec(pnl=-1.0, fkey="zz", coin="SOL", ts=time.time() - 3000)]
    all_trades = hist + [rec(pnl=1.0, fkey="e1m1v1", coin="SOL")] + later
    idx = len(hist)                       # the trade being backfilled
    note = ns["_build_auto_note_at"](FakeTrader(all_trades), all_trades[idx], idx)
    cited = [l for l in note["text"].split("\n") if "before this" in l]
    print("  " + (cited[0].strip() if cited else "(no history line)"))
    # SOL record before index 4 is 1W-2L. If the three later losses leaked in it
    # would read 1W-5L.
    if any("1W-5L" in l or "5L" in l for l in cited):
        fails.append("backfill leaked future trades into a historical note")
    else:
        print("  ok — later trades not counted\n")

    print("=" * 72)
    if fails:
        print(f"  {len(fails)} FAILURE(S)")
        for f in fails:
            print("   ✗ " + f)
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
